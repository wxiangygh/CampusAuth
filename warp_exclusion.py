"""
WARP 排除规则管理模块。
提供学习模式（DNS缓存监控）和基于 warp-cli tunnel host 的域名排除功能。
通过 warp-cli tunnel host add/remove/list 实现指定域名不走WARP（直连本地网络）。
相比IP/CIDR方案，域名排除无需解析IP、无需查询CIDR、无需重连WARP，速度极快。

网络方案（白名单域名不走 WARP，其他流量走 WARP）：
  IPv6 路由域名：tunnel host add + DNS fallback + IPv6 CIDR 排除 + 防火墙阻止 IPv4
                 → 域名排除 WARP，防火墙阻止 IPv4，浏览器只能走 IPv6 校园网直连
  IPv4 路由域名：tunnel host add
                 → 域名排除 WARP，走校园网 IPv4 直连
  其他流量：走 WARP 隧道（通过 IPv6 连接 Cloudflare），校园网只看到 IPv6 流量
"""
import os
import json
import socket
import threading
import time
import subprocess
import logging
from pathlib import Path
from core.command import run_command_simple

logger = logging.getLogger('warp_exclusion')

# 配置文件路径（与tray_app.py同目录）
SCRIPT_DIR = Path(__file__).parent.resolve()
if getattr(__import__('sys'), 'frozen', False):
    SCRIPT_DIR = Path(__import__('sys').executable).parent
EXCLUSION_CONFIG_FILE = SCRIPT_DIR / 'warp_exclusion_config.json'


def get_warp_cli_path():
    """查找 warp-cli 可执行文件路径"""
    code, _, _ = run_command_simple('warp-cli --version', shell=True)
    if code == 0:
        return 'warp-cli'
    default_paths = [
        r'C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe',
        r'C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe',
    ]
    for p in default_paths:
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------------------
# DNS 缓存监控（学习模式）
# ---------------------------------------------------------------------------

# 需要过滤的系统/本地域名后缀和关键词
_DOMAIN_FILTER_SUFFIXES = (
    '.local', '.localhost', '.home', '.lan', '.intranet',
    '.arpa', '.msftconnecttest.com', '.msftncsi.com',
)
_DOMAIN_FILTER_KEYWORDS = ('localhost', 'in-addr.arpa', 'ip6.arpa', 'wpad', 'isatap')


def _is_valid_domain(domain):
    """判断域名是否为有效的用户访问域名（过滤系统/本地域名）"""
    domain = domain.strip().rstrip('.').lower()
    if not domain or '.' not in domain:
        return False
    if len(domain) > 253:
        return False
    if any(domain.endswith(s) for s in _DOMAIN_FILTER_SUFFIXES):
        return False
    if any(kw in domain for kw in _DOMAIN_FILTER_KEYWORDS):
        return False
    if domain.replace('.', '').isdigit():
        return False
    return True


# ---------------------------------------------------------------------------
# 域名归一化与子域通配
# ---------------------------------------------------------------------------
# warp-cli 的 split tunnel 域名排除是**精确匹配**：`tunnel host add goofish.com`
# 只覆盖 goofish.com 本身，不覆盖 www.goofish.com / h5api.m.goofish.com 等子域。
# 必须额外写入 `*.goofish.com` 通配条目才能排除整个域名。
# （2026-09-01 实测：只加 goofish.com 时 Find-NetRoute 显示
#  203.119.169.227 -> WLAN，43.109.70.190(www) -> CloudflareWARP；
#  补上 *.goofish.com 后 www 也变成 WLAN。）
WILDCARD_PREFIX = '*.'


def normalize_domain_input(raw):
    """把用户粘贴的内容归一化为纯域名。

    支持：https://www.goofish.com/ 、http://x.com:8080/path?q=1#frag 、
          *.example.com 、EXAMPLE.COM. 、user@host
    返回小写、去端口、去路径、去尾点的域名；无效输入返回空串。
    """
    if raw is None:
        return ''
    value = str(raw).strip().strip('"\'')
    if not value:
        return ''
    value = value.split('#', 1)[0].split('?', 1)[0]
    if '//' in value:
        value = value.split('//', 1)[1]
    value = value.split('/', 1)[0]
    if '@' in value:
        value = value.rsplit('@', 1)[1]
    # 只有一个冒号时按 host:port 处理（IPv6 地址有多个冒号，保持原样）
    if value.count(':') == 1:
        value = value.rsplit(':', 1)[0]
    return value.strip().rstrip('.').lower()


def wildcard_host(host):
    """返回域名的通配子域形式：example.com -> *.example.com"""
    host = (host or '').strip().lower().rstrip('.')
    if not host:
        return ''
    if host.startswith(WILDCARD_PREFIX):
        return host
    if host.startswith('.'):
        host = host[1:]
    return WILDCARD_PREFIX + host


def base_host(host):
    """去掉通配前缀：*.example.com -> example.com"""
    host = (host or '').strip().lower().rstrip('.')
    return host[2:] if host.startswith(WILDCARD_PREFIX) else host


def flush_dns_cache():
    """刷新系统 DNS 缓存。

    WARP 的域名排除不是静态路由表，而是"观察到一次 DNS 查询后才下发绕行路由"。
    所以改完排除规则必须清缓存，否则浏览器沿用旧解析结果、长连接继续走 WARP。

    用 ipconfig /flushdns 而不是 PowerShell 的 Clear-DnsClientCache ——
    本机 PowerShell 冷启动约 5s，PowerShell 方案每次调用都要多等好几秒。
    """
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        subprocess.run('ipconfig /flushdns', shell=True, timeout=8,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       startupinfo=startupinfo,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return True
    except Exception as e:
        logger.debug(f'flush dns cache failed: {e}')
        return False


def get_dns_cache_domains():
    """通过 PowerShell Get-DnsClientCache 获取DNS缓存中的所有域名"""
    ps_cmd = 'Get-DnsClientCache | Select-Object -ExpandProperty Name | Sort-Object -Unique'
    code, output, _ = run_command_simple(['powershell', '-Command', ps_cmd], timeout=10)
    if code != 0 or not output:
        return []
    domains = set()
    for line in output.split('\n'):
        d = line.strip().rstrip('.')
        if _is_valid_domain(d):
            domains.add(d.lower())
    return sorted(domains)


class DnsMonitor:
    """DNS缓存监控器，后台线程定期轮询DNS缓存，记录用户访问的域名"""

    def __init__(self):
        self._learning = False
        self._thread = None
        self._lock = threading.Lock()
        self._learned_domains = set()
        self._poll_interval = 3.0

    @property
    def is_learning(self):
        return self._learning

    def start_learning(self):
        with self._lock:
            if self._learning:
                return False, '学习模式已在运行中'
            self._learning = True
            self._learned_domains.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info('DNS learning mode started')
        return True, '学习模式已启动，请正常访问需要排除的网站'

    def stop_learning(self):
        self._learning = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info(f'DNS learning stopped, captured {len(self._learned_domains)} domains')
        return True, f'学习完成，共捕获 {len(self._learned_domains)} 个域名'

    def _poll_loop(self):
        while self._learning:
            try:
                domains = get_dns_cache_domains()
                with self._lock:
                    self._learned_domains.update(domains)
            except Exception as e:
                logger.error(f'DNS poll error: {e}')
            waited = 0.0
            while self._learning and waited < self._poll_interval:
                time.sleep(0.5)
                waited += 0.5

    def get_learned_domains(self):
        with self._lock:
            return sorted(self._learned_domains)


# ---------------------------------------------------------------------------
# WARP 域名排除规则管理（基于 warp-cli tunnel host）
# ---------------------------------------------------------------------------
# 两种路由都用 tunnel host add 排除 WARP（不走 WARP）。
# IPv6 路由：额外 DNS fallback + IPv6 CIDR 排除 + 防火墙阻止 IPv4，
#           强制浏览器走 IPv6 校园网直连。
# IPv4 路由：走校园网 IPv4 直连。

def _make_dns_resolver():
    """创建 DNS 解析器，使用国内公共 DNS 绕过 WARP DNS 劫持。
    WARP 会接管系统 DNS，导致 socket.getaddrinfo 无法获取真实 AAAA 记录。
    使用 dnspython 直接查询 114.114.114.114 / 223.5.5.5，获取真实记录。
    返回 (resolver, dns_module) 或 (None, None)
    """
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        # 使用国内公共 DNS，避免 WARP DNS 劫持
        resolver.nameservers = ['114.114.114.114', '223.5.5.5']
        resolver.timeout = 3
        resolver.lifetime = 5
        return resolver, dns
    except ImportError:
        logger.warning('dnspython not available, DNS resolution may be inaccurate')
        return None, None


def _resolve_ipv6_prefixes(domain):
    """解析域名的 AAAA 记录，提取 /32 或 /48 前缀（用于 IPv6 CIDR 排除）。
    使用 dnspython 直接查询国内 DNS，绕过 WARP DNS 劫持。
    返回 CIDR 列表，如 ['240e:97d::/32', '240e:97d:10::/48']
    """
    prefixes = set()
    resolver, dns_mod = _make_dns_resolver()
    if resolver is None:
        # 回退到 socket.getaddrinfo（可能不准确）
        try:
            results = socket.getaddrinfo(domain, None, socket.AF_INET6)
            for result in results:
                ip = result[4][0]
                parts = ip.split(':')
                if len(parts) >= 4:
                    prefixes.add(':'.join(parts[:2]) + '::/32')
                    prefixes.add(':'.join(parts[:3]) + '::/48')
        except Exception as e:
            logger.debug(f'No AAAA records for {domain} (socket fallback): {e}')
        return list(prefixes)

    # 使用 dnspython 查询 AAAA 记录
    try:
        answers = resolver.resolve(domain, 'AAAA')
        for rdata in answers:
            ip = str(rdata)
            parts = ip.split(':')
            if len(parts) >= 4:
                # 提取 /32 和 /48 前缀，覆盖同网段的 CDN IP 轮换
                prefixes.add(':'.join(parts[:2]) + '::/32')
                prefixes.add(':'.join(parts[:3]) + '::/48')
        logger.debug(f'AAAA records for {domain} (dnspython): {[str(r) for r in answers]}')
    except dns_mod.resolver.NoAnswer:
        logger.debug(f'No AAAA records for {domain}: server returned no answer')
    except dns_mod.resolver.NXDOMAIN:
        logger.debug(f'No AAAA records for {domain}: domain does not exist')
    except Exception as e:
        logger.debug(f'No AAAA records for {domain}: {type(e).__name__}: {e}')
    return list(prefixes)


def _resolve_ipv4_addresses(domain):
    """解析域名的 A 记录，返回 IPv4 地址列表。
    使用 dnspython 直接查询国内 DNS，绕过 WARP DNS 劫持。
    """
    addrs = set()
    resolver, dns_mod = _make_dns_resolver()
    if resolver is None:
        # 回退到 socket.getaddrinfo
        try:
            results = socket.getaddrinfo(domain, None, socket.AF_INET)
            for result in results:
                addrs.add(result[4][0])
        except Exception as e:
            logger.debug(f'No A records for {domain} (socket fallback): {e}')
        return list(addrs)

    # 使用 dnspython 查询 A 记录
    try:
        answers = resolver.resolve(domain, 'A')
        for rdata in answers:
            addrs.add(str(rdata))
        logger.debug(f'A records for {domain} (dnspython): {list(addrs)}')
    except dns_mod.resolver.NoAnswer:
        logger.debug(f'No A records for {domain}: server returned no answer')
    except dns_mod.resolver.NXDOMAIN:
        logger.debug(f'No A records for {domain}: domain does not exist')
    except Exception as e:
        logger.debug(f'No A records for {domain}: {type(e).__name__}: {e}')
    return list(addrs)


# Hosts 文件管理：强制域名解析到 IPv6 地址
# 原因：WARP DNS fallback 不可靠，Chrome 用系统 DNS (127.0.2.2) 解析时拿不到 AAAA 记录
# 通过 hosts 文件可以直接指定 IPv6 地址，绕过 WARP DNS
HOSTS_FILE = r'C:\Windows\System32\drivers\etc\hosts'
HOSTS_MARKER_BEGIN = '# BEGIN CampusAuth IPv6 route'
HOSTS_MARKER_END = '# END CampusAuth IPv6 route'


def _add_ipv6_hosts_entry(domain, ipv6_addr):
    """添加 hosts 条目，强制域名解析到 IPv6 地址。
    需要管理员权限。返回 (success, message)
    """
    if not ipv6_addr:
        return False, '无 IPv6 地址'

    # 读取当前 hosts 文件
    try:
        with open(HOSTS_FILE, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        logger.error(f'Failed to read hosts file: {e}')
        return False, f'读取 hosts 失败: {e}'

    # 检查是否已有该域名的条目（在标记区域内）
    entry_line = f'{ipv6_addr} {domain}'
    if entry_line in content:
        logger.info(f'Hosts entry already exists: {entry_line}')
        return True, 'hosts 条目已存在'

    # 在标记区域内添加条目
    if HOSTS_MARKER_BEGIN in content:
        # 在标记区域末尾添加
        new_content = content.replace(
            HOSTS_MARKER_END,
            f'{entry_line}\n{HOSTS_MARKER_END}'
        )
    else:
        # 创建新的标记区域
        new_content = content.rstrip('\n') + '\n\n' + HOSTS_MARKER_BEGIN + '\n' + entry_line + '\n' + HOSTS_MARKER_END + '\n'

    # 写入临时文件，然后提权复制
    import tempfile
    import os
    tmp_file = os.path.join(tempfile.gettempdir(), f'hosts_{os.getpid()}_{int(time.time())}.txt')
    try:
        with open(tmp_file, 'w', encoding='utf-8') as f:
            f.write(new_content)
    except Exception as e:
        logger.error(f'Failed to write temp hosts file: {e}')
        return False, f'写入临时文件失败: {e}'

    # 提权复制到 hosts 文件
    code, output, err = _run_elevated_copy(tmp_file, HOSTS_FILE)
    try:
        os.remove(tmp_file)
    except Exception:
        pass

    if code == 0:
        logger.info(f'Hosts entry added: {entry_line}')
        # 清空 DNS 缓存，让新条目立即生效
        flush_dns_cache()
        return True, 'hosts 条目已添加'
    else:
        logger.error(f'Failed to copy hosts file: {err or output}')
        return False, f'复制 hosts 失败（需要管理员权限）: {err or output}'


def _remove_ipv6_hosts_entry(domain):
    """移除 hosts 文件中指定域名的 IPv6 条目。
    需要管理员权限。返回 (success, message)
    """
    try:
        with open(HOSTS_FILE, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        logger.error(f'Failed to read hosts file: {e}')
        return False, f'读取 hosts 失败: {e}'

    # 查找并移除该域名的条目
    lines = content.split('\n')
    new_lines = []
    removed = False
    for line in lines:
        # 匹配 "IPv6地址 域名" 格式（忽略注释和空行）
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1] == domain:
                removed = True
                continue  # 跳过这一行
        new_lines.append(line)

    if not removed:
        logger.info(f'No hosts entry found for {domain}')
        return True, 'hosts 中无该域名条目'

    new_content = '\n'.join(new_lines)

    # 写入临时文件，然后提权复制
    import tempfile
    import os
    tmp_file = os.path.join(tempfile.gettempdir(), f'hosts_{os.getpid()}_{int(time.time())}.txt')
    try:
        with open(tmp_file, 'w', encoding='utf-8') as f:
            f.write(new_content)
    except Exception as e:
        return False, f'写入临时文件失败: {e}'

    code, output, err = _run_elevated_copy(tmp_file, HOSTS_FILE)
    try:
        os.remove(tmp_file)
    except Exception:
        pass

    if code == 0:
        logger.info(f'Hosts entry removed for {domain}')
        flush_dns_cache()
        return True, 'hosts 条目已移除'
    else:
        return False, f'复制 hosts 失败: {err or output}'


def _run_elevated_copy(src, dst):
    """提权复制文件。返回 (exit_code, stdout, stderr)"""
    # 使用 PowerShell Start-Process 提权
    ps_cmd = f'Copy-Item -Path "{src}" -Destination "{dst}" -Force'
    code, output, err = run_command_simple(
        ['powershell', '-Command',
         f'Start-Process powershell -Verb RunAs -Wait -ArgumentList \'-Command\', \'{ps_cmd}\''],
        shell=False, timeout=30
    )
    return code, output, err


def _resolve_ipv6_addresses(domain):
    """解析域名的 AAAA 记录，返回 IPv6 地址列表（完整地址，非前缀）。
    使用 dnspython 直接查询国内 DNS，绕过 WARP DNS 劫持。
    """
    addrs = set()
    resolver, dns_mod = _make_dns_resolver()
    if resolver is None:
        try:
            results = socket.getaddrinfo(domain, None, socket.AF_INET6)
            for result in results:
                addrs.add(result[4][0])
        except Exception as e:
            logger.debug(f'No AAAA records for {domain} (socket fallback): {e}')
        return list(addrs)

    try:
        answers = resolver.resolve(domain, 'AAAA')
        for rdata in answers:
            addrs.add(str(rdata))
        logger.debug(f'AAAA addresses for {domain} (dnspython): {list(addrs)}')
    except dns_mod.resolver.NoAnswer:
        logger.debug(f'No AAAA records for {domain}: server returned no answer')
    except dns_mod.resolver.NXDOMAIN:
        logger.debug(f'No AAAA records for {domain}: domain does not exist')
    except Exception as e:
        logger.debug(f'No AAAA records for {domain}: {type(e).__name__}: {e}')
    return list(addrs)


def _add_ipv4_firewall_block(domain, ipv4_addrs):
    """为指定域名添加 IPv4 防火墙阻止规则（所有协议）。
    强制浏览器走 IPv6，阻止 IPv4 直连。
    返回 (success, blocked_ipv4_list)
    """
    if not ipv4_addrs:
        return True, []

    # 收集需要阻止的 IPv4 地址和 /24 子网
    blocked = set()
    for addr in ipv4_addrs:
        blocked.add(addr)
        # 添加 /24 子网，覆盖同网段的 CDN IP 轮换
        parts = addr.split('.')
        if len(parts) == 4:
            blocked.add(f'{parts[0]}.{parts[1]}.{parts[2]}.0/24')

    blocked_list = sorted(blocked)
    remote_addrs = ','.join(blocked_list)

    # 规则名称包含域名，便于管理和移除
    rule_name = f"CampusAuth_IPv6Route_{domain}"

    # 先移除已有规则（避免重复）
    run_command_simple(['powershell', '-Command',
        f'Remove-NetFirewallRule -DisplayName "{rule_name}" -ErrorAction SilentlyContinue'],
        timeout=10)

    # 创建新规则：阻止所有协议的出站 IPv4 流量
    code, output, err = run_command_simple([
        'powershell', '-Command',
        f'New-NetFirewallRule -DisplayName "{rule_name}" '
        f'-Direction Outbound -Action Block '
        f'-RemoteAddress {remote_addrs} -Profile Any'
    ], timeout=15)

    if code == 0:
        logger.info(f'IPv4 firewall block added for {domain}: {blocked_list}')
        return True, blocked_list

    msg = (output + err).strip()[:200]
    # 尝试提权
    if 'Access is denied' in msg or '拒绝访问' in msg:
        logger.info(f'IPv4 firewall block for {domain} needs elevation, retrying...')
        code2, _, _ = run_command_simple([
            'powershell', '-Command',
            f'Start-Process powershell -Verb RunAs -Wait -ArgumentList '
            f'"-Command", "New-NetFirewallRule -DisplayName \\"{rule_name}\\" '
            f'-Direction Outbound -Action Block '
            f'-RemoteAddress {remote_addrs} -Profile Any"'
        ], timeout=30)
        if code2 == 0:
            logger.info(f'IPv4 firewall block added for {domain} (elevated): {blocked_list}')
            return True, blocked_list

    logger.warning(f'Failed to add IPv4 firewall block for {domain}: {msg}')
    return False, []


def _remove_ipv4_firewall_block(domain):
    """移除指定域名的 IPv4 防火墙阻止规则"""
    rule_name = f"CampusAuth_IPv6Route_{domain}"
    code, output, err = run_command_simple([
        'powershell', '-Command',
        f'Remove-NetFirewallRule -DisplayName "{rule_name}" -ErrorAction SilentlyContinue'
    ], timeout=10)
    if code == 0:
        logger.info(f'IPv4 firewall block removed for {domain}')
        return True
    # 尝试提权
    msg = (output + err).strip()[:200]
    if 'Access is denied' in msg or '拒绝访问' in msg:
        code2, _, _ = run_command_simple([
            'powershell', '-Command',
            f'Start-Process powershell -Verb RunAs -Wait -ArgumentList '
            f'"-Command", "Remove-NetFirewallRule -DisplayName \\"{rule_name}\\" -ErrorAction SilentlyContinue"'
        ], timeout=30)
        if code2 == 0:
            logger.info(f'IPv4 firewall block removed for {domain} (elevated)')
            return True
    return False


def _remove_all_ipv4_firewall_blocks():
    """移除所有 CampusAuth 创建的防火墙规则"""
    run_command_simple(['powershell', '-Command',
        'Remove-NetFirewallRule -DisplayName "CampusAuth_IPv6Route_*" -ErrorAction SilentlyContinue'],
        timeout=15)
    return True


def _list_ipv6_route_firewall_domains():
    """列出存在 CampusAuth_IPv6Route_* 防火墙规则的域名（用于推断 IPv6 路由）。
    返回域名集合；查询失败时返回空集合（调用方按 IPv4 路由处理，副作用最小）。
    """
    ps_cmd = ('Get-NetFirewallRule -DisplayName "CampusAuth_IPv6Route_*" '
              '-ErrorAction SilentlyContinue | Select-Object -ExpandProperty DisplayName')
    code, output, _ = run_command_simple(['powershell', '-Command', ps_cmd], timeout=15)
    domains = set()
    if code != 0 or not output:
        return domains
    prefix = 'CampusAuth_IPv6Route_'
    for line in output.split('\n'):
        name = line.strip()
        if name.startswith(prefix):
            domains.add(name[len(prefix):].lower())
    return domains


# ---------------------------------------------------------------------------
# WARP 底层 IPv6 常驻 pin（程序级防火墙规则）
# ---------------------------------------------------------------------------
# 问题：WARP 连接后应用会恢复 conf.json 的 IPv4 端点，此后 WARP 自行重连时
# （网络变化、掉线重连等）一旦 WLAN 的 IPv4 处于启用状态，端点选择会偏好
# IPv4，底层就从 IPv6 切到 IPv4——所有 WARP 流量变成校园网 IPv4 流量，
# 且校园网到 Cloudflare 的 IPv4 通路不稳定，表现为"Cloudflare 经常断开"。
# （2026-09-01 用户实测：开启 IPv4 后底层自动从 IPv6 切成 IPv4。）
# 方案：对 warp-svc.exe 添加程序级出站阻止规则，屏蔽 Cloudflare 端点的
# IPv4 地址段，WARP 只能使用 IPv6 端点。规则只作用于 warp-svc.exe，
# 不影响其他程序（DoH/API 走 104.16.x，不在屏蔽段内，不受影响）。

WARP_UNDERLAY_PIN_RULE = 'CampusAuth_WARPv6Underlay'
# Cloudflare WARP 端点 IPv4 段：162.158.0.0/15 覆盖 162.159.192-198（当前
# conf.json 端点为 162.159.198.2，engage 域名的 A 记录在该段内轮换），
# 188.114.96.0/20 覆盖 188.114.96-99 备用端点段。
WARP_ENDPOINT_IPV4_RANGES = ('162.158.0.0/15', '188.114.96.0/20')


def _find_warp_svc_path():
    """查找 warp-svc.exe 路径（防火墙程序级规则需要完整路径）"""
    for p in (r'C:\Program Files\Cloudflare\Cloudflare WARP\warp-svc.exe',
              r'C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-svc.exe'):
        if os.path.isfile(p):
            return p
    return None


def is_warp_underlay_pinned():
    """检查 WARP 底层 IPv6 pin 防火墙规则是否存在"""
    code, output, _ = run_command_simple([
        'powershell', '-Command',
        f'if (Get-NetFirewallRule -DisplayName "{WARP_UNDERLAY_PIN_RULE}" '
        f'-ErrorAction SilentlyContinue) {{ "yes" }} else {{ "no" }}'
    ], timeout=15)
    return code == 0 and output.strip().lower() == 'yes'


def ensure_warp_underlay_ipv6_pin():
    """创建 WARP 底层 IPv6 pin 防火墙规则（幂等）。返回 (success, message)"""
    svc = _find_warp_svc_path()
    if not svc:
        return False, 'warp-svc.exe 未找到'
    remote_addrs = ','.join(WARP_ENDPOINT_IPV4_RANGES)
    # 先删后建，保证 RemoteAddress/Program 与当前定义一致
    run_command_simple(['powershell', '-Command',
        f'Remove-NetFirewallRule -DisplayName "{WARP_UNDERLAY_PIN_RULE}" -ErrorAction SilentlyContinue'],
        timeout=15)
    code, output, err = run_command_simple([
        'powershell', '-Command',
        f'New-NetFirewallRule -DisplayName "{WARP_UNDERLAY_PIN_RULE}" '
        f'-Direction Outbound -Action Block -Program "{svc}" '
        f'-RemoteAddress {remote_addrs} -Profile Any'
    ], timeout=15)
    if code == 0:
        logger.info(f'WARP underlay IPv6 pin added (program={svc}, blocked={remote_addrs})')
        return True, 'WARP 底层已固定为 IPv6'
    msg = (output + err).strip()[:200]
    if 'Access is denied' in msg or '拒绝访问' in msg:
        code2, _, err2 = run_command_simple([
            'powershell', '-Command',
            f'Start-Process powershell -Verb RunAs -Wait -ArgumentList '
            f'"-Command", "New-NetFirewallRule -DisplayName \\"{WARP_UNDERLAY_PIN_RULE}\\" '
            f'-Direction Outbound -Action Block -Program \\"{svc}\\" '
            f'-RemoteAddress {remote_addrs} -Profile Any"'
        ], timeout=30)
        if code2 == 0:
            logger.info('WARP underlay IPv6 pin added (elevated)')
            return True, 'WARP 底层已固定为 IPv6（提权）'
        msg = (msg + ' ' + (err2 or '').strip())[:200]
    logger.warning(f'Failed to add WARP underlay pin: {msg}')
    return False, f'WARP 底层 IPv6 pin 添加失败: {msg}'


def remove_warp_underlay_ipv6_pin():
    """移除 WARP 底层 IPv6 pin 防火墙规则。返回 (success, message)"""
    code, output, err = run_command_simple([
        'powershell', '-Command',
        f'Remove-NetFirewallRule -DisplayName "{WARP_UNDERLAY_PIN_RULE}" -ErrorAction SilentlyContinue'
    ], timeout=15)
    if code == 0:
        logger.info('WARP underlay IPv6 pin removed')
        return True, 'WARP 底层 IPv6 pin 已移除'
    msg = (output + err).strip()[:200]
    if 'Access is denied' in msg or '拒绝访问' in msg:
        code2, _, _ = run_command_simple([
            'powershell', '-Command',
            f'Start-Process powershell -Verb RunAs -Wait -ArgumentList '
            f'"-Command", "Remove-NetFirewallRule -DisplayName \\"{WARP_UNDERLAY_PIN_RULE}\\" '
            f'-ErrorAction SilentlyContinue"'
        ], timeout=30)
        if code2 == 0:
            logger.info('WARP underlay IPv6 pin removed (elevated)')
            return True, 'WARP 底层 IPv6 pin 已移除（提权）'
    logger.warning(f'Failed to remove WARP underlay pin: {msg}')
    return False, f'移除失败: {msg}'


def warp_add_host(host, route='ipv6'):
    """添加域名排除规则。
    route='ipv6': tunnel host add + DNS fallback + IPv6 CIDR 排除 + 防火墙阻止 IPv4。
                  域名排除 WARP（IPv4/IPv6 都不走 WARP），
                  防火墙阻止 IPv4 → 浏览器只能走 IPv6 → 校园网 IPv6 直连。
    route='ipv4': tunnel host add + DNS fallback。
                  域名排除 WARP，走校园网 IPv4 直连。
    返回 (success, message, blocked_ipv4)
    """
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return False, 'warp-cli 未找到', []

    blocked_ipv4 = []

    # 两种路由都用 tunnel host add，确保域名排除 WARP（不走 WARP）。
    # tunnel host 是精确匹配，必须再补一条 *.host 通配规则才能覆盖子域，
    # 否则浏览器访问 www.host 时依然走 WARP。
    add_targets = [host]
    wildcard = wildcard_host(host)
    if wildcard and wildcard != host:
        add_targets.append(wildcard)

    for target in add_targets:
        code, output, err = run_command_simple([warp_cli, 'tunnel', 'host', 'add', target], shell=False)
        if code == 0:
            logger.info(f'WARP add host {target}: success')
            continue
        msg = (output + err).strip()[:200]
        if 'already' in msg.lower() or '已存在' in msg:
            logger.info(f'WARP add host {target}: already exists')
            continue
        logger.warning(f'WARP add host {target}: failed, {msg}')
        # 根域名写不进去才算失败；通配条目失败不阻断（子域只是覆盖面变窄）
        if target == host:
            return False, f'添加失败: {msg}', []
        logger.warning(f'Subdomain wildcard {target} rejected by warp-cli, '
                       f'{host} 的子域不会被排除')

    # 两种路由都加 DNS fallback：域名既已排除 WARP，连接走校园网直连，
    # DNS 也必须走本地解析，否则 WARP DoH 会把 CDN 域名解析到海外节点
    # （2026-09-01 实测：goofish.com 经 WARP DNS 解析到 43.109.70.x 海外
    # 阿里云节点，经本地 DNS 解析到 155.102.180.x 国内节点），
    # 直连海外节点慢且易被判为"排除无效"。
    code, output, err = run_command_simple([warp_cli, 'dns', 'fallback', 'add', host], shell=False)
    if code == 0:
        logger.info(f'WARP add dns fallback {host} ({route} route): success')
    else:
        logger.debug(f'WARP add dns fallback {host}: {(output+err).strip()[:100]}')

    if route == 'ipv6':
        # IPv6 路由额外操作：
        # 先检查域名是否有 IPv6 (AAAA) 记录，没有则无法走 IPv6 直连
        ipv6_prefixes = _resolve_ipv6_prefixes(host)
        ipv6_addrs = _resolve_ipv6_addresses(host)
        ipv4_addrs = _resolve_ipv4_addresses(host)

        if not ipv6_prefixes:
            # 域名没有 IPv6 地址，无法走 IPv6 直连
            # 原因：WARP 排除的流量会绕过 Windows 防火墙，防火墙阻止 IPv4 无效
            # 所以只能降级为 IPv4 路由（走校园网 IPv4 直连）
            logger.warning(f'{host} has no AAAA records, cannot use IPv6 route, falling back to IPv4')
            flush_dns_cache()
            return True, f'域名 {host} 无 IPv6 地址，已自动改为 IPv4 直连模式（WARP 排除+IPv4 直连）', []

        # 1. IPv6 CIDR 排除：让 IPv6 流量走校园网直连
        # （DNS fallback 已在上方对两种路由统一添加）
        for cidr in ipv6_prefixes:
            c, o, e = run_command_simple([warp_cli, 'tunnel', 'ip', 'add-range', cidr], shell=False)
            if c == 0:
                logger.info(f'WARP auto-exclude IPv6 CIDR {cidr} for {host}: success')
            else:
                logger.debug(f'WARP auto-exclude IPv6 CIDR {cidr} for {host}: skipped ({(o+e).strip()[:100]})')

        # 2. 添加 hosts 条目，强制域名解析到 IPv6 地址
        # 这是关键步骤！WARP DNS fallback 不可靠，Chrome 用系统 DNS (127.0.2.2) 解析时拿不到 AAAA 记录
        # 通过 hosts 文件可以直接指定 IPv6 地址，绕过 WARP DNS
        if ipv6_addrs:
            # 只用第一个 IPv6 地址（避免 hosts 文件过长）
            ipv6_addr = ipv6_addrs[0]
            ok, hosts_msg = _add_ipv6_hosts_entry(host, ipv6_addr)
            if ok:
                logger.info(f'Hosts entry added for {host}: {ipv6_addr}')
            else:
                logger.warning(f'Failed to add hosts entry for {host}: {hosts_msg}')
        else:
            logger.warning(f'No IPv6 addresses for {host}, cannot add hosts entry')

        # 3. 防火墙阻止 IPv4（所有协议），强制浏览器走 IPv6
        # 注意：WARP 排除的流量可能绕过 Windows 防火墙，此规则不一定生效
        # 但仍尝试添加，对非 WARP 排除的 IPv4 流量有效
        if ipv4_addrs:
            ok, blocked = _add_ipv4_firewall_block(host, ipv4_addrs)
            if ok:
                blocked_ipv4 = blocked
                logger.info(f'IPv4 firewall block added for {host}: {blocked}')
            else:
                logger.warning(f'Failed to add IPv4 firewall block for {host}, browser may still use IPv4')
        else:
            logger.info(f'No IPv4 addresses for {host}, no firewall block needed')

    # 排除规则只有在 WARP 观察到一次 DNS 查询后才会下发绕行路由，
    # 清掉缓存才能让浏览器下次访问时立刻走到直连路径。
    flush_dns_cache()
    return True, '添加成功', blocked_ipv4


def warp_remove_host(host, route='ipv6'):
    """删除域名排除规则。
    route='ipv6': tunnel host remove + DNS fallback remove + IPv6 CIDR remove + IPv4 防火墙移除
    route='ipv4': tunnel host remove + DNS fallback remove
    """
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return False, 'warp-cli 未找到'

    # 移除根域名与通配子域两条条目（添加时会成对写入）
    remove_targets = [host]
    wildcard = wildcard_host(host)
    if wildcard and wildcard != host:
        remove_targets.append(wildcard)

    for target in remove_targets:
        code, output, err = run_command_simple([warp_cli, 'tunnel', 'host', 'remove', target], shell=False)
        if code == 0:
            logger.info(f'WARP remove host {target}: success')
        else:
            msg = (output + err).strip()
            if 'Not found' in msg or 'not found' in msg.lower():
                logger.info(f'WARP remove host {target}: not found, treated as success')
            else:
                logger.warning(f'WARP remove host {target}: failed, {msg[:200]}')

    # 两种路由都添加了 DNS fallback（见 warp_add_host），移除时一并清理
    code, output, err = run_command_simple([warp_cli, 'dns', 'fallback', 'remove', host], shell=False)
    if code == 0:
        logger.info(f'WARP remove dns fallback {host}: success')
    else:
        logger.debug(f'WARP remove dns fallback {host}: {(output+err).strip()[:100]}')

    if route == 'ipv6':
        # IPv6 路由额外清理
        # 2. 移除 IPv6 CIDR
        ipv6_prefixes = _resolve_ipv6_prefixes(host)
        for cidr in ipv6_prefixes:
            c, o, e = run_command_simple([warp_cli, 'tunnel', 'ip', 'remove-range', cidr], shell=False)
            if c == 0:
                logger.info(f'WARP auto-remove IPv6 CIDR {cidr} for {host}: success')
            else:
                logger.debug(f'WARP auto-remove IPv6 CIDR {cidr} for {host}: skipped')

        # 3. 移除 hosts 条目
        ok, hosts_msg = _remove_ipv6_hosts_entry(host)
        if ok:
            logger.info(f'Hosts entry removed for {host}')
        else:
            logger.debug(f'Failed to remove hosts entry for {host}: {hosts_msg}')

        # 4. 移除 IPv4 防火墙阻止规则
        _remove_ipv4_firewall_block(host)

    flush_dns_cache()
    return True, '删除成功'


def warp_list_hosts():
    """列出当前所有WARP域名排除规则：warp-cli tunnel host list"""
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return []
    code, output, _ = run_command_simple([warp_cli, 'tunnel', 'host', 'list'], shell=False)
    if code != 0:
        return []
    # 解析输出，格式如：
    # Excluded hosts:
    #   www.baidu.com (CLI exclude)
    hosts = []
    for line in output.split('\n'):
        line = line.strip()
        if not line or line.startswith('Excluded hosts'):
            continue
        # 提取域名部分（去掉 "(CLI exclude)" 等标注）
        host = line.split()[0] if line.split() else ''
        if host and '.' in host:
            hosts.append(host)
    return hosts


def warp_list_ip_ranges():
    """列出当前所有WARP IP排除规则（warp-cli tunnel ip list），返回 (CLI添加的规则列表, 全部规则列表)"""
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return [], []
    code, output, _ = run_command_simple([warp_cli, 'tunnel', 'ip', 'list'], shell=False)
    if code != 0:
        return [], []
    cli_ranges = []  # 用户通过CLI添加的规则（带 CLI exclude 标注）
    all_ranges = []  # 全部规则（含系统默认）
    for line in output.split('\n'):
        line = line.strip()
        if not line or line.startswith('Excluded routes'):
            continue
        all_ranges.append(line)
        if 'CLI exclude' in line:
            cidr = line.split()[0] if line.split() else ''
            if '/' in cidr:
                cli_ranges.append(cidr)
    return cli_ranges, all_ranges


def warp_add_ip_range(cidr):
    """添加WARP IP排除规则：warp-cli tunnel ip add-range <cidr>

    与 warp_add_ip（单 IP，tunnel ip add）区分：本函数用于 CIDR 网段。
    """
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return False, 'warp-cli 未找到'
    code, output, err = run_command_simple([warp_cli, 'tunnel', 'ip', 'add-range', cidr], shell=False)
    if code == 0:
        logger.info(f'WARP add ip range {cidr}: success')
        return True, '添加成功'
    msg = (output + err).strip()[:200]
    if 'already' in msg.lower() or '已存在' in msg:
        return True, '已存在'
    logger.warning(f'WARP add ip range {cidr}: failed, {msg}')
    return False, f'添加失败: {msg}'


def warp_remove_ip_range(cidr):
    """删除WARP IP排除规则：warp-cli tunnel ip remove-range <cidr>"""
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return False, 'warp-cli 未找到'
    code, output, err = run_command_simple([warp_cli, 'tunnel', 'ip', 'remove-range', cidr], shell=False)
    if code == 0:
        logger.info(f'WARP remove ip range {cidr}: success')
        return True, '删除成功'
    msg = (output + err).strip()[:200]
    logger.warning(f'WARP remove ip range {cidr}: failed, {msg}')
    return False, f'删除失败: {msg}'


def warp_add_ip(ip):
    """添加单个 IP 排除规则：warp-cli tunnel ip add <ip>
    用于无域名的连接，直接排除 IP 不走 WARP。
    """
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return False, 'warp-cli 未找到'
    code, output, err = run_command_simple([warp_cli, 'tunnel', 'ip', 'add', ip], shell=False)
    if code == 0:
        logger.info(f'WARP add ip {ip}: success')
        return True, '添加成功'
    msg = (output + err).strip()[:200]
    if 'already' in msg.lower() or '已存在' in msg:
        return True, '已存在'
    logger.warning(f'WARP add ip {ip}: failed, {msg}')
    return False, f'添加失败: {msg}'


def warp_remove_ip(ip):
    """移除单个 IP 排除规则：warp-cli tunnel ip remove <ip>"""
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return False, 'warp-cli 未找到'
    code, output, err = run_command_simple([warp_cli, 'tunnel', 'ip', 'remove', ip], shell=False)
    if code == 0:
        logger.info(f'WARP remove ip {ip}: success')
        return True, '移除成功'
    msg = (output + err).strip()[:200]
    if 'not found' in msg.lower() or 'Not found' in msg:
        return True, '不存在'
    logger.warning(f'WARP remove ip {ip}: failed, {msg}')
    return False, f'移除失败: {msg}'


def warp_cleanup_cli_ip_ranges():
    """
    清理通过CLI添加的IP排除规则中属于"旧版残留"的部分。
    以下规则不算残留，跳过清理：
    1. IPv6 白名单域名（route='ipv6'）自动排除的 IPv6 CIDR
    2. IP 范围管理中用户手动添加的 CIDR
    仅清理：不属于上述两类的孤立规则。
    返回 (success, message, details)
    """
    cli_ranges, _ = warp_list_ip_ranges()
    if not cli_ranges:
        return True, '无需清理（没有CLI添加的IP规则）', []

    cfg = load_exclusion_config()
    # 收集域名规则自动生成的 IPv6 CIDR
    active_ipv6_cidrs = set()
    for entry in cfg.get('domains', []):
        if entry.get('enabled', True) and entry.get('route', 'ipv6') == 'ipv6':
            prefixes = _resolve_ipv6_prefixes(entry['domain'])
            active_ipv6_cidrs.update(prefixes)
    # 收集 IP 范围管理中启用的 CIDR
    active_ip_range_cidrs = set()
    for entry in cfg.get('ip_ranges', []):
        if entry.get('enabled', True):
            active_ip_range_cidrs.add(entry['cidr'])

    details = []
    success_count = 0
    fail_count = 0
    skipped_count = 0
    for cidr in cli_ranges:
        if cidr in active_ipv6_cidrs or cidr in active_ip_range_cidrs:
            skipped_count += 1
            logger.debug(f'Skip active CIDR: {cidr}')
            continue
        ok, msg = warp_remove_ip_range(cidr)
        details.append({'cidr': cidr, 'success': ok, 'message': msg})
        if ok:
            success_count += 1
        else:
            fail_count += 1
    if skipped_count:
        overall_msg = f'清理完成: 删除 {success_count} 条, 跳过 {skipped_count} 条(使用中), {fail_count} 失败'
    else:
        overall_msg = f'清理完成: 删除 {success_count} 条IP规则, {fail_count} 失败'
    return fail_count == 0, overall_msg, details


# ---------------------------------------------------------------------------
# WARP DNS Fallback 域名管理（基于 warp-cli dns fallback）
# ---------------------------------------------------------------------------
# 用途：让指定域名的 DNS 查询走本地运营商 DNS，而非 WARP DNS（cloudflare-dns.com）。
# 典型场景：CDN 调度域名（如 bytedns3.com）通过 WARP DNS 解析会返回海外 CDN 节点，
# 导致中国用户访问被阻止。加入 DNS fallback 后，这些域名走本地 DNS，返回大陆节点。
# 与 tunnel host 的区别：
#   - tunnel host: 排除流量（连接不走 WARP）
#   - dns fallback: 排除 DNS 查询（解析走本地 DNS，流量仍可走 WARP）

def warp_add_dns_fallback(domain):
    """添加域名到 DNS fallback：warp-cli dns fallback add <domain>
    让该域名的 DNS 查询走本地 DNS，而非 WARP DNS。
    """
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return False, 'warp-cli 未找到'
    code, output, err = run_command_simple([warp_cli, 'dns', 'fallback', 'add', domain], shell=False)
    if code == 0:
        logger.info(f'WARP add dns fallback {domain}: success')
        return True, '添加成功'
    msg = (output + err).strip()[:200]
    logger.warning(f'WARP add dns fallback {domain}: failed, {msg}')
    return False, f'添加失败: {msg}'


def warp_remove_dns_fallback(domain):
    """从 DNS fallback 移除域名：warp-cli dns fallback remove <domain>
    "Not found" 视为成功（域名本就不在 fallback 列表中）。
    """
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return False, 'warp-cli 未找到'
    code, output, err = run_command_simple([warp_cli, 'dns', 'fallback', 'remove', domain], shell=False)
    if code == 0:
        logger.info(f'WARP remove dns fallback {domain}: success')
        return True, '删除成功'
    msg = (output + err).strip()
    if 'Not found' in msg or 'not found' in msg.lower():
        logger.info(f'WARP remove dns fallback {domain}: not found, treated as success')
        return True, '已移除（原本不存在）'
    logger.warning(f'WARP remove dns fallback {domain}: failed, {msg[:200]}')
    return False, f'删除失败: {msg[:200]}'


def warp_list_dns_fallback():
    """列出当前所有 DNS fallback 域名：warp-cli dns fallback list
    只返回通过 CLI 添加的域名（带 "CLI add" 标注），过滤掉系统默认项。
    """
    warp_cli = get_warp_cli_path()
    if not warp_cli:
        return []
    code, output, _ = run_command_simple([warp_cli, 'dns', 'fallback', 'list'], shell=False)
    if code != 0:
        return []
    # 输出格式：
    # Fallback domains:
    #   corp
    #   bytedns3.com (CLI add)
    domains = []
    for line in output.split('\n'):
        line = line.strip()
        if not line or line.startswith('Fallback domains'):
            continue
        # 只收集 CLI 添加的域名（用户自定义），跳过系统默认项
        if 'CLI add' in line:
            domain = line.split()[0] if line.split() else ''
            if domain and '.' in domain:
                domains.append(domain)
    return domains


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------

def load_exclusion_config():
    """加载排除规则配置
    配置结构：
    {
        'domains': [{domain, enabled, added_at}],  # tunnel host 流量排除
        'dns_fallback': [{domain, enabled, added_at}]  # dns fallback DNS查询排除
    }
    """
    _seed_exclusion_config()
    defaults = {'domains': [], 'dns_fallback': [], 'ip_ranges': []}
    if EXCLUSION_CONFIG_FILE.exists():
        try:
            with open(EXCLUSION_CONFIG_FILE, encoding='utf-8') as f:
                cfg = json.load(f)
                # 合并默认值，确保新字段存在
                merged = {**defaults, **cfg}
                if 'dns_fallback' not in merged:
                    merged['dns_fallback'] = []
                return merged
        except Exception as e:
            logger.error(f'Failed to load exclusion config: {e}')
    return defaults


def _seed_exclusion_config():
    """首次运行时用打包内置模板播种分流规则配置（不含敏感信息）。"""
    if EXCLUSION_CONFIG_FILE.exists():
        return
    try:
        import shutil
        if getattr(__import__('sys'), 'frozen', False):
            tpl = Path(__import__('sys')._MEIPASS) / 'config' / 'warp_exclusion_config.json'
        else:
            tpl = Path(__file__).resolve().parent / 'config' / 'warp_exclusion_config.json'
        if tpl.exists():
            shutil.copyfile(tpl, EXCLUSION_CONFIG_FILE)
            logger.info('Exclusion config seeded from bundled template')
    except Exception as e:
        logger.debug(f'Seed exclusion config failed: {e}')


def save_exclusion_config(cfg):
    """保存排除规则配置"""
    try:
        with open(EXCLUSION_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        logger.info('Exclusion config saved')
        return True
    except Exception as e:
        logger.error(f'Failed to save exclusion config: {e}')
        return False


# ---------------------------------------------------------------------------
# 排除规则管理器
# ---------------------------------------------------------------------------

class ExclusionManager:
    """
    排除规则管理器（基于 warp-cli tunnel host 域名排除）。
    配置格式：
    {
        "domains": [
            {"domain": "example.com", "enabled": true, "added_at": "2026-06-20 12:00:00"}
        ]
    }
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.dns_monitor = DnsMonitor()
        self._collapse_dirty = False
        # 启动时从 WARP 同步规则到本地配置（确保 WARP 中的规则在应用中可见可管理）
        self.sync_from_warp()

    def sync_from_warp(self, kind=None):
        """从 WARP 读取当前规则，合并到本地配置中。
        解决场景：应用重新打包后本地配置丢失，但 WARP 中的规则仍在，
        导致用户无法通过应用界面管理这些规则。
        合并策略：WARP 中存在但本地不存在的规则会被添加进来（enabled=True），
        本地已有的规则不会被覆盖。

        kind: 只同步某一类规则，供界面按子tab分别触发
            None/'all'   全部（域名 + DNS fallback + IP 范围）
            'domain'     仅域名排除（tunnel host）
            'dns'        仅 DNS fallback 域名
            'ip'         仅 IP/CIDR 范围
        返回 (success, message, details)
        """
        if kind in (None, '', 'all'):
            sync_hosts = sync_dns = sync_ip = True
        elif kind in ('domain', 'dns', 'ip'):
            sync_hosts, sync_dns, sync_ip = (
                kind == 'domain', kind == 'dns', kind == 'ip')
        else:
            return False, f'无效的同步类型: {kind}', {}

        with self._lock:
            cfg = load_exclusion_config()
            details = {'hosts_added': [], 'dns_added': [], 'ip_ranges_added': []}
            # 折叠通配条目由域名分支负责；限定其他类型时不应带上上一轮遗留的脏标记
            self._collapse_dirty = False

            # 同步 tunnel host 规则
            if sync_hosts:
                warp_hosts = warp_list_hosts()
                existing_domains = {base_host(d['domain']) for d in cfg.get('domains', [])}
                self._collapse_wildcard_entries(cfg, existing_domains)
                # 判断路由类型：IPv6 路由的域名会创建 CampusAuth_IPv6Route_* 防火墙规则。
                # （不能用 DNS fallback 判别：2026-09-01 起 IPv4 路由域名也会添加
                #  fallback，以保证排除域名经本地 DNS 解析到国内 CDN 节点。）
                new_hosts = []
                for host in warp_hosts:
                    # `*.x.com` 与 `x.com` 是同一条规则的两个条目，只登记根域名
                    host = base_host(host)
                    if host and host not in existing_domains:
                        new_hosts.append(host)
                        existing_domains.add(host)
                ipv6_route_domains = _list_ipv6_route_firewall_domains() if new_hosts else set()
                for host in new_hosts:
                    route = 'ipv6' if host in ipv6_route_domains else 'ipv4'
                    entry = {
                        'domain': host,
                        'enabled': True,
                        'route': route,
                        'added_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'source': 'warp_sync',
                    }
                    cfg.setdefault('domains', []).append(entry)
                    existing_domains.add(host)
                    details['hosts_added'].append(host)
                    logger.info(f'Synced tunnel host from WARP ({route} route): {host}')

            # 同步 DNS fallback 规则
            if sync_dns:
                warp_dns = warp_list_dns_fallback()
                # DNS fallback 中已有的独立管理条目
                existing_dns = {d['domain'] for d in cfg.get('dns_fallback', [])}
                # 域名排除列表中已有的域名（不管 route）
                existing_domain_names = {d['domain'] for d in cfg.get('domains', [])}
                for domain in warp_dns:
                    if domain in existing_domain_names:
                        # 已在域名排除列表中，跳过（可能是 IPv6 路由域名自动添加的 DNS fallback）
                        continue
                    if domain not in existing_dns:
                        entry = {
                            'domain': domain,
                            'enabled': True,
                            'added_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                            'source': 'warp_sync',
                        }
                        cfg.setdefault('dns_fallback', []).append(entry)
                        existing_dns.add(domain)
                        details['dns_added'].append(domain)
                        logger.info(f'Synced dns fallback from WARP: {domain}')

            # 同步 IP 范围规则（CLI 添加的 CIDR 排除）
            if sync_ip:
                cli_ranges, _ = warp_list_ip_ranges()
                # 收集域名规则自动生成的 IPv6 CIDR，这些不算独立的 IP 范围规则
                domain_ipv6_cidrs = set()
                for entry in cfg.get('domains', []):
                    if entry.get('enabled', True) and entry.get('route', 'ipv6') == 'ipv6':
                        prefixes = _resolve_ipv6_prefixes(entry['domain'])
                        domain_ipv6_cidrs.update(prefixes)
                existing_ip_ranges = {r['cidr'] for r in cfg.get('ip_ranges', [])}
                for cidr in cli_ranges:
                    if cidr not in existing_ip_ranges and cidr not in domain_ipv6_cidrs:
                        # 根据地址类型自动判断 route
                        route = 'ipv6' if ':' in cidr else 'ipv4'
                        entry = {
                            'cidr': cidr,
                            'route': route,
                            'enabled': True,
                            'added_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                            'source': 'warp_sync',
                        }
                        cfg.setdefault('ip_ranges', []).append(entry)
                        existing_ip_ranges.add(cidr)
                        details['ip_ranges_added'].append(cidr)
                        logger.info(f'Synced IP range from WARP: {cidr}')

            # 只有实际新增了规则或清理了重复项才保存
            has_new = (details['hosts_added'] or details['dns_added']
                       or details['ip_ranges_added'] or self._collapse_dirty)
            self._collapse_dirty = False
            if has_new:
                self._save_config(cfg)
                total = len(details['hosts_added']) + len(details['dns_added']) + len(details['ip_ranges_added'])
                msg = f'从 WARP 同步了 {total} 条规则'
                logger.info(msg)
                return True, msg, details
            else:
                return True, 'WARP 与本地配置已同步，无需更新', details

    def _collapse_wildcard_entries(self, cfg, existing_domains=None):
        """把配置里 `*.x.com` 形式的重复条目收敛为根域名条目。

        排除规则现在按「根域名 + 通配子域」成对下发，配置里只需要保留根域名。
        老配置（或从 WARP 同步进来的通配条目）里可能残留 `*.x.com`，
        这里统一折叠掉，避免界面出现两行看起来重复的规则。
        """
        self._collapse_dirty = False
        domains = cfg.get('domains', [])
        if not domains:
            self._collapse_dirty = False
            return
        base_names = {base_host(d['domain']) for d in domains}
        if existing_domains:
            base_names |= set(existing_domains)
        collapsed = []
        seen = set()
        for entry in domains:
            name = entry['domain']
            base = base_host(name)
            if not base:
                continue
            if base in seen:
                continue
            seen.add(base)
            if name != base:
                entry = dict(entry)
                entry['domain'] = base
            collapsed.append(entry)
        if len(collapsed) != len(domains):
            cfg['domains'] = collapsed
            self._collapse_dirty = True
            logger.info(f'Collapsed {len(domains) - len(collapsed)} wildcard domain entries')

    def get_config(self):
        with self._lock:
            return load_exclusion_config()

    def _save_config(self, cfg):
        save_exclusion_config(cfg)

    def add_domain(self, domain, route='ipv6'):
        """
        添加域名到排除列表并立即应用到WARP。
        route='ipv4': tunnel host add，走校园网 IPv4 直连
        route='ipv6': tunnel host add + DNS fallback + IPv6 CIDR + 阻止 IPv4，走校园网 IPv6 直连
        返回 (success, message, info)
        """
        # 允许直接粘贴网址：https://www.goofish.com/ -> goofish.com
        domain = normalize_domain_input(domain)
        domain = base_host(domain)
        if not domain or '.' not in domain:
            return False, '域名格式无效（可填 goofish.com 或直接粘贴网址）', None
        # www 只是站点的一个主机名，排除站点时应该落到注册域上，
        # 这样 www / m / api 等所有子域才会被 *.domain 通配规则覆盖。
        stripped_www = ''
        if domain.startswith('www.') and domain.count('.') >= 2:
            stripped_www = domain
            domain = domain[4:]
        if route not in ('ipv4', 'ipv6'):
            route = 'ipv6'
        with self._lock:
            cfg = load_exclusion_config()
            for entry in cfg['domains']:
                if base_host(entry['domain']) == domain:
                    return False, f'域名 {domain} 已存在', None
            # 立即应用到WARP
            ok, msg, blocked_ipv4 = warp_add_host(domain, route=route)
            if not ok:
                return False, msg, None
            # 如果域名无 IPv6 地址，warp_add_host 会自动降级为 IPv4 路由
            # 此时 msg 包含"已自动改为 IPv4 直连模式"，需要同步配置中的 route
            actual_route = route
            if route == 'ipv6' and '无 IPv6 地址' in msg:
                actual_route = 'ipv4'
            entry = {
                'domain': domain,
                'enabled': True,
                'route': actual_route,
                'added_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            if blocked_ipv4:
                entry['blocked_ipv4'] = blocked_ipv4
            cfg['domains'].append(entry)
            self._save_config(cfg)
            logger.info(f'Added domain {domain} (route={actual_route})')
            if stripped_www:
                msg = f'{msg}（{stripped_www} 已归并为 {domain}，子域一并排除）'
            return True, msg, entry

    def remove_domain(self, domain):
        """从排除列表移除域名，并从WARP删除规则"""
        domain = base_host(normalize_domain_input(domain))
        with self._lock:
            cfg = load_exclusion_config()
            # 用户可能填 www.x.com 而配置里存的是 x.com（或反过来），两边都认
            names = [base_host(d['domain']) for d in cfg['domains']]
            if domain not in names:
                alt = domain[4:] if domain.startswith('www.') else 'www.' + domain
                if alt in names:
                    domain = alt
            # 查找该域名的 route，用于决定是否清理 IPv6 CIDR
            route = 'ipv6'
            for entry in cfg['domains']:
                if base_host(entry['domain']) == domain:
                    route = entry.get('route', 'ipv6')
                    break
            original_len = len(cfg['domains'])
            cfg['domains'] = [d for d in cfg['domains'] if base_host(d['domain']) != domain]
            if len(cfg['domains']) == original_len:
                return False, f'域名 {domain} 不存在'
            self._save_config(cfg)
            # 从WARP删除规则
            warp_remove_host(domain, route=route)
            logger.info(f'Removed domain {domain}')
            return True, f'域名 {domain} 已移除'

    def toggle_domain(self, domain, enabled):
        """启用/禁用某个域名的排除规则（同步WARP状态）"""
        with self._lock:
            cfg = load_exclusion_config()
            for entry in cfg['domains']:
                if base_host(entry['domain']) == domain:
                    domain = base_host(entry['domain'])
                    old_enabled = entry.get('enabled', True)
                    if old_enabled == enabled:
                        return True, f'域名 {domain} 状态未变化'
                    entry['enabled'] = enabled
                    self._save_config(cfg)
                    # 同步WARP规则：启用则添加，禁用则删除
                    route = entry.get('route', 'ipv6')
                    if enabled:
                        ok, _, blocked_ipv4 = warp_add_host(domain, route=route)
                        if blocked_ipv4:
                            entry['blocked_ipv4'] = blocked_ipv4
                            self._save_config(cfg)
                    else:
                        warp_remove_host(domain, route=route)
                    return True, f'域名 {domain} 已{"启用" if enabled else "禁用"}'
            return False, f'域名 {domain} 不存在'

    def set_domain_route(self, domain, route):
        """修改域名的路由类型（ipv4/ipv6），同步 WARP 规则。
        从 ipv4 改为 ipv6：需要添加 DNS fallback + IPv6 CIDR + 阻止 IPv4
        从 ipv6 改为 ipv4：需要移除 DNS fallback + IPv6 CIDR + IPv4 阻止
        """
        if route not in ('ipv4', 'ipv6'):
            return False, f'无效的路由类型: {route}'
        with self._lock:
            cfg = load_exclusion_config()
            for entry in cfg['domains']:
                if base_host(entry['domain']) == domain:
                    domain = base_host(entry['domain'])
                    old_route = entry.get('route', 'ipv6')
                    if old_route == route:
                        return True, f'域名 {domain} 路由未变化'
                    # 更新配置
                    entry['route'] = route
                    # 同步 WARP：先移除旧规则，再添加新规则
                    warp_remove_host(domain, route=old_route)
                    if entry.get('enabled', True):
                        ok, _, blocked_ipv4 = warp_add_host(domain, route=route)
                        if blocked_ipv4:
                            entry['blocked_ipv4'] = blocked_ipv4
                        elif 'blocked_ipv4' in entry:
                            del entry['blocked_ipv4']
                    self._save_config(cfg)
                    logger.info(f'Changed route for {domain}: {old_route} -> {route}')
                    return True, f'域名 {domain} 已切换为走{route.upper()}校园网'
            return False, f'域名 {domain} 不存在'

    def set_domain_mode(self, domain, mode):
        """兼容旧API：域名排除方案无需模式选择，直接返回成功"""
        return True, '域名排除方案无需选择模式', None

    def check_ipv6_support(self):
        """检测所有 route='ipv6' 的域名是否真的支持 IPv6 (AAAA 记录)。
        对于不支持 IPv6 的域名，自动降级为 IPv4 路由，并清理无效的防火墙规则。
        返回 (success, message, details)
        """
        with self._lock:
            cfg = load_exclusion_config()
            details = []
            fixed_count = 0
            for entry in cfg.get('domains', []):
                if entry.get('route', 'ipv6') != 'ipv6':
                    continue
                if not entry.get('enabled', True):
                    continue
                domain = entry['domain']
                # 检测是否有 IPv6 地址
                ipv6_prefixes = _resolve_ipv6_prefixes(domain)
                if ipv6_prefixes:
                    details.append({'domain': domain, 'has_ipv6': True, 'action': 'kept'})
                    continue
                # 无 IPv6 地址，降级为 IPv4
                old_route = entry.get('route', 'ipv6')
                entry['route'] = 'ipv4'
                # 清理无效的 IPv6 CIDR 排除规则（如果有）
                # 清理无效的 IPv4 防火墙阻止规则
                _remove_ipv4_firewall_block(domain)
                fixed_count += 1
                details.append({
                    'domain': domain,
                    'has_ipv6': False,
                    'action': 'downgraded',
                    'message': '无 IPv6 地址，已降级为 IPv4 直连'
                })
                logger.info(f'Domain {domain} downgraded: ipv6 -> ipv4 (no AAAA records)')
            if fixed_count > 0:
                self._save_config(cfg)
                msg = f'检测完成: {fixed_count} 个域名无 IPv6 支持，已降级为 IPv4 直连'
            else:
                msg = '检测完成: 所有 IPv6 路由域名均支持 IPv6'
            return True, msg, details

    # ------------------------------------------------------------------
    # IP 范围排除管理（warp-cli tunnel ip add-range/remove-range）
    # ------------------------------------------------------------------

    def add_ip_range(self, cidr, route='ipv4'):
        """添加 IP/CIDR 到排除列表并立即应用到 WARP。
        route='ipv4': 标记为走 IPv4（CIDR 排除 WARP，配合全局 IPv4 阻止）
        route='ipv6': 标记为走 IPv6（CIDR 排除 WARP，走校园网 IPv6 直连）
        返回 (success, message, info)
        """
        cidr = cidr.strip()
        if not cidr or '/' not in cidr:
            return False, 'CIDR 格式无效，如 1.2.3.0/24 或 240d:c000::/32', None
        if route not in ('ipv4', 'ipv6'):
            route = 'ipv4'
        with self._lock:
            cfg = load_exclusion_config()
            for entry in cfg.get('ip_ranges', []):
                if entry['cidr'] == cidr:
                    return False, f'IP 范围 {cidr} 已存在', None
            # 立即应用到 WARP
            warp_cli = get_warp_cli_path()
            if warp_cli:
                code, output, err = run_command_simple([warp_cli, 'tunnel', 'ip', 'add-range', cidr], shell=False)
                if code != 0:
                    msg = (output + err).strip()[:200]
                    return False, f'添加失败: {msg}', None
            entry = {
                'cidr': cidr,
                'route': route,
                'enabled': True,
                'added_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            cfg.setdefault('ip_ranges', []).append(entry)
            self._save_config(cfg)
            logger.info(f'Added IP range {cidr} (route={route})')
            return True, f'IP 范围 {cidr} 已添加（走{route.upper()}校园网）', entry

    def remove_ip_range(self, cidr):
        """从排除列表移除 IP/CIDR，并从 WARP 删除规则"""
        with self._lock:
            cfg = load_exclusion_config()
            original_len = len(cfg.get('ip_ranges', []))
            cfg['ip_ranges'] = [r for r in cfg.get('ip_ranges', []) if r['cidr'] != cidr]
            if len(cfg['ip_ranges']) == original_len:
                return False, f'IP 范围 {cidr} 不存在'
            self._save_config(cfg)
            # 从 WARP 删除
            warp_remove_ip_range(cidr)
            logger.info(f'Removed IP range {cidr}')
            return True, f'IP 范围 {cidr} 已移除'

    def toggle_ip_range(self, cidr, enabled):
        """启用/禁用某个 IP 范围的排除规则"""
        with self._lock:
            cfg = load_exclusion_config()
            for entry in cfg.get('ip_ranges', []):
                if entry['cidr'] == cidr:
                    old_enabled = entry.get('enabled', True)
                    if old_enabled == enabled:
                        return True, f'IP 范围 {cidr} 状态未变化'
                    entry['enabled'] = enabled
                    self._save_config(cfg)
                    if enabled:
                        warp_cli = get_warp_cli_path()
                        if warp_cli:
                            run_command_simple([warp_cli, 'tunnel', 'ip', 'add-range', cidr], shell=False)
                    else:
                        warp_remove_ip_range(cidr)
                    return True, f'IP 范围 {cidr} 已{"启用" if enabled else "禁用"}'
            return False, f'IP 范围 {cidr} 不存在'

    def set_ip_range_route(self, cidr, route):
        """修改 IP 范围的路由类型"""
        if route not in ('ipv4', 'ipv6'):
            return False, f'无效的路由类型: {route}'
        with self._lock:
            cfg = load_exclusion_config()
            for entry in cfg.get('ip_ranges', []):
                if entry['cidr'] == cidr:
                    old_route = entry.get('route', 'ipv4')
                    if old_route == route:
                        return True, f'IP 范围 {cidr} 路由未变化'
                    entry['route'] = route
                    self._save_config(cfg)
                    logger.info(f'Changed route for IP range {cidr}: {old_route} -> {route}')
                    return True, f'IP 范围 {cidr} 已切换为走{route.upper()}校园网'
            return False, f'IP 范围 {cidr} 不存在'

    def refresh_domain(self, domain):
        """兼容旧API：域名排除方案无需刷新IP解析"""
        with self._lock:
            cfg = load_exclusion_config()
            for entry in cfg['domains']:
                if entry['domain'] == domain:
                    entry['last_refreshed'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    self._save_config(cfg)
                    return True, f'域名 {domain} 已刷新', entry
            return False, f'域名 {domain} 不存在', None

    def refresh_all(self):
        """兼容旧API：域名排除方案无需刷新IP解析"""
        cfg = load_exclusion_config()
        results = [{'domain': d['domain'], 'success': True, 'message': '无需刷新'} for d in cfg['domains']]
        return True, '域名排除方案无需刷新', results

    def apply_to_warp(self, domain=None):
        """
        将排除规则应用到WARP。
        域名排除方案中，添加域名时已即时应用，此方法用于同步配置与WARP状态。
        返回 (success, message, details)
        """
        with self._lock:
            cfg = load_exclusion_config()
            targets = cfg['domains']
            if domain:
                targets = [d for d in targets if d['domain'] == domain]
            if not targets:
                return False, '没有排除规则', []
            details = []
            success_count = 0
            fail_count = 0
            for entry in targets:
                route = entry.get('route', 'ipv6')
                if entry.get('enabled', True):
                    ok, msg, blocked_ipv4 = warp_add_host(entry['domain'], route=route)
                    if blocked_ipv4:
                        entry['blocked_ipv4'] = blocked_ipv4
                else:
                    ok, msg = warp_remove_host(entry['domain'], route=route)
                details.append({
                    'domain': entry['domain'],
                    'range': '',
                    'success': ok,
                    'message': msg,
                })
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
            # 保存可能更新的 blocked_ipv4
            self._save_config(cfg)
            overall_msg = f'应用完成: {success_count} 成功, {fail_count} 失败'
            return fail_count == 0, overall_msg, details

    def remove_from_warp(self, domain):
        """从WARP移除指定域名的排除规则"""
        ok, msg = warp_remove_host(domain)
        return ok, msg, [{'domain': domain, 'range': '', 'success': ok, 'message': msg}]

    def get_warp_ranges(self):
        """获取当前WARP中的所有域名排除规则"""
        return warp_list_hosts()

    # ------------------------------------------------------------------
    # DNS Fallback 域名管理（warp-cli dns fallback）
    # ------------------------------------------------------------------
    # 用途：让 CDN 调度域名（如 bytedns3.com）的 DNS 查询走本地运营商 DNS，
    # 避免 WARP DNS 返回海外 CDN 节点导致中国用户访问被阻止。
    # 与 tunnel host 互补：tunnel host 排除流量，dns fallback 排除 DNS 查询。

    def add_dns_fallback(self, domain):
        """添加域名到 DNS fallback 列表并立即应用到 WARP。
        返回 (success, message, info)
        """
        domain = domain.strip().lower()
        if not domain or '.' not in domain:
            return False, '域名格式无效', None
        with self._lock:
            cfg = load_exclusion_config()
            dns_list = cfg.get('dns_fallback', [])
            for entry in dns_list:
                if entry['domain'] == domain:
                    return False, f'域名 {domain} 已存在', None
            # 立即应用到 WARP
            ok, msg = warp_add_dns_fallback(domain)
            if not ok:
                return False, msg, None
            entry = {
                'domain': domain,
                'enabled': True,
                'added_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            dns_list.append(entry)
            cfg['dns_fallback'] = dns_list
            self._save_config(cfg)
            logger.info(f'Added dns fallback {domain}')
            return True, f'域名 {domain} 已添加到 DNS fallback', entry

    def remove_dns_fallback(self, domain):
        """从 DNS fallback 列表移除域名，并从 WARP 删除规则"""
        with self._lock:
            cfg = load_exclusion_config()
            dns_list = cfg.get('dns_fallback', [])
            original_len = len(dns_list)
            cfg['dns_fallback'] = [d for d in dns_list if d['domain'] != domain]
            if len(cfg['dns_fallback']) == original_len:
                return False, f'域名 {domain} 不存在'
            self._save_config(cfg)
            # 从 WARP 删除规则（即使配置中不存在也尝试删除，清理残留）
            warp_remove_dns_fallback(domain)
            logger.info(f'Removed dns fallback {domain}')
            return True, f'域名 {domain} 已从 DNS fallback 移除'

    def toggle_dns_fallback(self, domain, enabled):
        """启用/禁用某个 DNS fallback 域名（同步 WARP 状态）"""
        with self._lock:
            cfg = load_exclusion_config()
            dns_list = cfg.get('dns_fallback', [])
            for entry in dns_list:
                if entry['domain'] == domain:
                    old_enabled = entry.get('enabled', True)
                    if old_enabled == enabled:
                        return True, f'域名 {domain} 状态未变化'
                    entry['enabled'] = enabled
                    self._save_config(cfg)
                    if enabled:
                        warp_add_dns_fallback(domain)
                    else:
                        warp_remove_dns_fallback(domain)
                    return True, f'域名 {domain} 已{"启用" if enabled else "禁用"}'
            return False, f'域名 {domain} 不存在'

    def apply_dns_fallback_to_warp(self):
        """将所有启用的 DNS fallback 域名同步到 WARP。
        返回 (success, message, details)
        """
        with self._lock:
            cfg = load_exclusion_config()
            dns_list = cfg.get('dns_fallback', [])
            if not dns_list:
                return True, '没有 DNS fallback 规则', []
            details = []
            success_count = 0
            fail_count = 0
            for entry in dns_list:
                if entry.get('enabled', True):
                    ok, msg = warp_add_dns_fallback(entry['domain'])
                else:
                    ok, msg = warp_remove_dns_fallback(entry['domain'])
                details.append({
                    'domain': entry['domain'],
                    'success': ok,
                    'message': msg,
                })
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
            overall_msg = f'同步完成: {success_count} 成功, {fail_count} 失败'
            return fail_count == 0, overall_msg, details

    def get_dns_fallback_list(self):
        """获取当前 WARP 中所有 CLI 添加的 DNS fallback 域名"""
        return warp_list_dns_fallback()

    # ------------------------------------------------------------------
    # IP 范围同步（与 DNS fallback 对称，供界面"IP 排除"子tab使用）
    # ------------------------------------------------------------------

    def apply_ip_ranges_to_warp(self):
        """将所有 IP/CIDR 排除规则同步到 WARP（tunnel ip add-range / remove-range）。

        启用的条目执行 add-range，禁用的条目执行 remove-range，
        用于把本地配置与 WARP 实时状态对齐（例如重启后规则残留/缺失）。
        返回 (success, message, details)
        """
        with self._lock:
            cfg = load_exclusion_config()
            ranges = cfg.get('ip_ranges', [])
            if not ranges:
                return True, '没有 IP 排除规则', []
            details = []
            success_count = 0
            fail_count = 0
            for entry in ranges:
                cidr = entry.get('cidr')
                if not cidr:
                    continue
                if entry.get('enabled', True):
                    ok, msg = warp_add_ip_range(cidr)
                else:
                    ok, msg = warp_remove_ip_range(cidr)
                details.append({
                    'cidr': cidr,
                    'success': ok,
                    'message': msg,
                })
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
            overall_msg = f'同步完成: {success_count} 成功, {fail_count} 失败'
            return fail_count == 0, overall_msg, details

    # ------------------------------------------------------------------
    # IPv4 启用/禁用管理（WLAN 适配器级别）
    # ------------------------------------------------------------------
    # 注意：这与全局 IPv4 阻止（防火墙级别）不同。
    # WLAN 适配器禁用 IPv4 会完全移除 IPv4 协议栈，影响所有应用。
    # 全局 IPv4 阻止只是防火墙阻止出站 IPv4 流量，更精细。

    def is_ipv4_enabled(self):
        """检查 WLAN 适配器是否启用了 IPv4"""
        return is_wlan_ipv4_enabled()

    def set_ipv4_enabled(self, enabled):
        """启用或禁用 WLAN 适配器的 IPv4
        返回 (success, message)
        """
        if enabled:
            ok, msg = enable_wlan_ipv4()
        else:
            ok, msg = disable_wlan_ipv4()
        if ok:
            # 保存状态到配置
            with self._lock:
                cfg = load_exclusion_config()
                cfg['ipv4_enabled'] = enabled
                self._save_config(cfg)
        return ok, msg


# ---------------------------------------------------------------------------
# IPv4 适配器管理
# ---------------------------------------------------------------------------

def _find_wlan_adapter():
    """查找 WLAN 适配器名称，优先找 Intel Wireless，其次找 WLAN"""
    try:
        code, output, _ = run_command_simple(
            ['powershell', '-Command',
             'Get-NetAdapter | Where-Object {$_.Status -eq "Up" -and $_.InterfaceDescription -like "*Wireless*"} | Select-Object -ExpandProperty Name'],
            timeout=10
        )
        if code == 0 and output.strip():
            return output.strip().split('\n')[0].strip()
    except Exception:
        pass
    return 'WLAN'


def is_wlan_ipv4_enabled():
    """检查 WLAN 适配器的 IPv4 是否启用"""
    adapter = _find_wlan_adapter()
    try:
        code, output, _ = run_command_simple(
            ['powershell', '-Command',
             f'(Get-NetAdapterBinding -Name "{adapter}" -ComponentID ms_tcpip).Enabled'],
            timeout=10
        )
        if code == 0 and output.strip().lower() == 'true':
            return True
    except Exception:
        pass
    return False


def enable_wlan_ipv4():
    """启用 WLAN 适配器的 IPv4（需要管理员权限）"""
    adapter = _find_wlan_adapter()
    code, output, err = run_command_simple(
        ['powershell', '-Command',
         f'Enable-NetAdapterBinding -Name "{adapter}" -ComponentID ms_tcpip'],
        timeout=15
    )
    if code == 0:
        logger.info(f'IPv4 enabled on {adapter}')
        return True, 'IPv4 已启用'
    msg = (output + err).strip()[:200]
    # 权限不足时用提权方式重试
    if 'Access is denied' in msg or '拒绝访问' in msg:
        code2, _, _ = run_command_simple(
            ['powershell', '-Command',
             f'Start-Process powershell -Verb RunAs -ArgumentList \'-Command\', \'Enable-NetAdapterBinding -Name "{adapter}" -ComponentID ms_tcpip\' -Wait'],
            timeout=30
        )
        if code2 == 0:
            logger.info(f'IPv4 enabled on {adapter} (elevated)')
            return True, 'IPv4 已启用（提权）'
    logger.warning(f'Failed to enable IPv4: {msg}')
    return False, f'启用 IPv4 失败: {msg}'


def disable_wlan_ipv4():
    """禁用 WLAN 适配器的 IPv4（需要管理员权限）"""
    adapter = _find_wlan_adapter()
    code, output, err = run_command_simple(
        ['powershell', '-Command',
         f'Disable-NetAdapterBinding -Name "{adapter}" -ComponentID ms_tcpip'],
        timeout=15
    )
    if code == 0:
        logger.info(f'IPv4 disabled on {adapter}')
        return True, 'IPv4 已禁用'
    msg = (output + err).strip()[:200]
    if 'Access is denied' in msg or '拒绝访问' in msg:
        code2, _, _ = run_command_simple(
            ['powershell', '-Command',
             f'Start-Process powershell -Verb RunAs -ArgumentList \'-Command\', \'Disable-NetAdapterBinding -Name "{adapter}" -ComponentID ms_tcpip\' -Wait'],
            timeout=30
        )
        if code2 == 0:
            logger.info(f'IPv4 disabled on {adapter} (elevated)')
            return True, 'IPv4 已禁用（提权）'
    logger.warning(f'Failed to disable IPv4: {msg}')
    return False, f'禁用 IPv4 失败: {msg}'


# 全局单例
_exclusion_manager = None
_exclusion_manager_lock = threading.Lock()


def get_exclusion_manager():
    """获取 ExclusionManager 全局单例"""
    global _exclusion_manager
    with _exclusion_manager_lock:
        if _exclusion_manager is None:
            _exclusion_manager = ExclusionManager()
        return _exclusion_manager

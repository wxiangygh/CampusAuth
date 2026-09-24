"""DNS presets and validation shared by settings and network operations."""
import ipaddress
import re

# 定向解析规则条数上限，以及域名目标的匹配格式（至少两级、顶级域为字母）
MAX_BINDING_RULES = 64
_DOMAIN_RE = re.compile(r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')


DNS_PRESETS = [
    {'id': 'legacy', 'label': '默认（114 DNS + 阿里云）',
     'servers': ['114.114.114.114', '223.5.5.5'],
     'ipv6_servers': ['2606:4700:4700::1111', '2606:4700:4700::1001']},
    {'id': 'aliyun', 'label': '中国 · 阿里云公共 DNS',
     'servers': ['223.5.5.5', '223.6.6.6'],
     'ipv6_servers': ['2400:3200::1', '2400:3200:baba::1']},
    {'id': 'unicom_beijing', 'label': '中国 · 北京联通（地区 DNS）',
     'servers': ['123.123.123.123', '123.123.123.124'], 'ipv6_servers': []},
    {'id': '114', 'label': '中国 · 114 DNS',
     'servers': ['114.114.114.114', '114.114.115.115'], 'ipv6_servers': []},
    {'id': 'google', 'label': '国外 · Google 公共 DNS',
     'servers': ['8.8.8.8', '8.8.4.4'],
     'ipv6_servers': ['2001:4860:4860::8888', '2001:4860:4860::8844']},
    {'id': 'cloudflare', 'label': '国外 · Cloudflare',
     'servers': ['1.1.1.1', '1.0.0.1'],
     'ipv6_servers': ['2606:4700:4700::1111', '2606:4700:4700::1001']},
]


def validate_servers(value, *, ipv6_only=False):
    if isinstance(value, str):
        value = [x for x in re.split(r'[\s,，;；]+', value.strip()) if x]
    if not isinstance(value, list) or len(value) > 6:
        raise ValueError('DNS 地址必须为列表，最多填写 6 个 IP 地址')
    result = []
    for item in value:
        try:
            address = ipaddress.ip_address(str(item).strip())
        except ValueError:
            raise ValueError(f'无效的 DNS IP 地址：{item}') from None
        if ipv6_only and address.version != 6:
            raise ValueError('认证 IPv6 DNS 只能填写 IPv6 地址')
        if address.is_unspecified or address.is_multicast:
            raise ValueError(f'不能使用此 DNS 地址：{item}')
        if str(address) not in result:
            result.append(str(address))
    if not result and not ipv6_only:
        raise ValueError('请至少填写一个 DNS 服务器地址')
    return result


def dns_settings(config):
    return {
        'preset': config.get('dns_preset', 'legacy'),
        'servers': validate_servers(config.get('dns_servers', DNS_PRESETS[0]['servers'])),
        'ipv6_servers': validate_servers(
            config.get('dns_ipv6_servers', DNS_PRESETS[0]['ipv6_servers']), ipv6_only=True),
    }


# ---------------------------------------------------------------------------
# 定向解析绑定：为特定域名 / IP / CIDR 指定解析所用的 DNS 服务器
# ---------------------------------------------------------------------------
# 目标是三类字符串之一，保存时统一规范化（小写、去通配符与末尾点）：
#   域名 example.com  —— 同时匹配其所有子域名
#   IP   1.2.3.4 / 2400:3200::1
#   网段 1.2.3.0/24 / 2400:3200::/32

def validate_binding_target(value):
    """规范化绑定目标；非法输入抛 ValueError。"""
    text = str(value or '').strip().rstrip('.').lower()
    if not text:
        raise ValueError('绑定目标不能为空')
    bare = text[2:] if text.startswith('*.') else text
    try:
        return str(ipaddress.ip_address(bare))
    except ValueError:
        pass
    try:
        return str(ipaddress.ip_network(bare, strict=False))
    except ValueError:
        pass
    if not _DOMAIN_RE.match(bare):
        raise ValueError(f'无效的绑定目标：{value}（需为域名、IP 或 CIDR）')
    return bare


def validate_bindings(value):
    """校验并规范化绑定规则列表，返回 [{'target','servers','enabled'}]。"""
    if not isinstance(value, list):
        raise ValueError('DNS 绑定规则格式无效')
    if len(value) > MAX_BINDING_RULES:
        raise ValueError(f'DNS 绑定规则最多 {MAX_BINDING_RULES} 条')
    rules, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('DNS 绑定规则格式无效')
        target = validate_binding_target(item.get('target'))
        if target in seen:
            raise ValueError(f'绑定目标重复：{target}')
        seen.add(target)
        rules.append({'target': target,
                      'servers': validate_servers(item.get('servers')),
                      'enabled': bool(item.get('enabled', True))})
    return rules


def dns_bindings(config):
    """读取配置中的绑定规则；内容损坏时按无规则处理，不阻塞其它 DNS 设置。"""
    try:
        return validate_bindings(config.get('dns_bindings') or [])
    except ValueError:
        return []


def _as_address(value):
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None


def _split_rules(rules):
    """按目标类型分组，并丢弃手改配置里残缺或已禁用的规则。

    返回 (网段规则, 域名规则)，元素均为 (匹配值, 服务器列表)。
    """
    ip_rules, domain_rules = [], []
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get('enabled', True):
            continue
        target = str(rule.get('target') or '').strip().lower()
        servers = [str(s) for s in (rule.get('servers') or []) if s]
        if not target or not servers:
            continue
        try:
            ip_rules.append((ipaddress.ip_network(target, strict=False), servers))
        except ValueError:
            domain_rules.append((target, servers))
    return ip_rules, domain_rules


def _best_ip_match(ip_rules, addresses):
    """取覆盖任一地址的最具体规则（前缀最长者）。"""
    best = None
    for net, servers in ip_rules:
        for addr in addresses:
            if addr.version != net.version or addr not in net:
                continue
            if best is None or net.prefixlen > best[0]:
                best = (net.prefixlen, servers)
    return list(best[1]) if best else []


def binding_servers(rules, target, addresses=()):
    """返回 target 应使用的解析服务器；没有适用规则时返回 []。

    target 是域名时按最长后缀匹配（子域名继承父域规则）；
    target 是 IP 时匹配 IP/网段规则；
    addresses 给出该域名已知的解析结果，用于「结果落在某网段 → 换服务器重查」的网段规则。
    """
    text = str(target or '').strip().rstrip('.').lower()
    if not text or not rules:
        return []
    ip_rules, domain_rules = _split_rules(rules)

    address = _as_address(text)
    if address is not None:
        return _best_ip_match(ip_rules, [address])

    matched = [(name, servers) for name, servers in domain_rules
               if text == name or text.endswith('.' + name)]
    if matched:
        return list(max(matched, key=lambda item: len(item[0]))[1])

    known = [a for a in (_as_address(item) for item in addresses) if a]
    return _best_ip_match(ip_rules, known) if known else []

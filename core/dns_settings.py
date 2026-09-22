"""DNS presets and validation shared by settings and network operations."""
import ipaddress
import re


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

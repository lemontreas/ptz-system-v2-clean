"""
S9 星链设备识别模块

判定规则：三条特征全部命中才标记为星链，任一未命中均不判定。
  F1: TPC Report (IE ID=35) 第1字节 == 63
      原理：星链固件将 6-bit TPC 字段硬编码为极限值 0b111111=63，
            合规民用 AP 发射功率范围仅 15~30 dBm。
  F2: WMM IE (OUI=00:50:f2 type=02) elt.info[6]==0x00 且 elt.info[12]==0x23
      原理：SpaceX 固件改写了 WMM QoS 策略字节，
            普通路由器对应位置为 0x80 / 0x27，零重叠。
  F3: Tag221 数量恰好为3，OUI组合严格为 {00:50:f2, 00:0c:43, 00:0c:e7}
      原理：星链固件极度精简，去除普通路由器的所有附加私有标签，
            仅保留 WMM + MediaTek短包 + MediaTek长包三联结构。

排他性验证：实测 ceshi_13.pcap / ceshi_149.pcap，背景环境设备零重叠。
"""

import logging

logger = logging.getLogger(__name__)

# 星链 Vendor Specific OUI 严格组合
_STARLINK_VENDOR_OUIS = frozenset([
    b'\x00\x50\xf2',   # Microsoft WMM
    b'\x00\x0c\x43',   # MediaTek 短包
    b'\x00\x0c\xe7',   # MediaTek 长包
])


def _iter_elts(pkt):
    """遍历帧中所有 Dot11Elt 层"""
    from scapy.all import Dot11Elt
    elt = pkt.getlayer(Dot11Elt)
    while elt and isinstance(elt, Dot11Elt):
        yield elt
        elt = elt.payload.getlayer(Dot11Elt) if elt.payload else None


def _check_f1_tpc(pkt) -> bool:
    """F1: TPC Report IE (ID=35) 第1字节 == 63"""
    for elt in _iter_elts(pkt):
        if elt.ID == 35 and len(elt.info) >= 1:
            return elt.info[0] == 63
    return False


def _check_f2_wmm(pkt) -> bool:
    """
    F2: WMM IE (ID=221, OUI=00:50:f2 type=02)
    完整 IE 序列（含 tag+length）:
      dd 18 00 50 f2 02 01 01 [6]00 00 03 a4 00 00 [12]23 a4 ...
    Scapy elt.info 从 OUI 首字节开始（跳过 tag 和 length 两字节）:
      elt.info[6]  对应完整 IE 第9字节  → 星链=0x00，普通路由器=0x80
      elt.info[12] 对应完整 IE 第15字节 → 星链=0x23，普通路由器=0x27
    """
    for elt in _iter_elts(pkt):
        if (elt.ID == 221
                and len(elt.info) >= 13
                and elt.info[:4] == b'\x00\x50\xf2\x02'):
            return elt.info[6] == 0x00 and elt.info[12] == 0x23
    return False


def _check_f3_vendor(pkt) -> bool:
    """
    F3: Tag221 数量恰好为3，OUI组合严格为 {00:50:f2, 00:0c:43, 00:0c:e7}
    普通联发科路由器通常携带 5~6 条 Tag221。
    """
    vendor_ouis = []
    for elt in _iter_elts(pkt):
        if elt.ID == 221 and len(elt.info) >= 3:
            vendor_ouis.append(bytes(elt.info[:3]))
    return len(vendor_ouis) == 3 and set(vendor_ouis) == _STARLINK_VENDOR_OUIS


_MAX_ANALYZE_PER_BSSID = 20  # 每个 BSSID 最多分析多少个 Beacon 帧


class StarLinkDetector:
    """
    星链设备识别器。

    策略：每个 BSSID 最多分析 _MAX_ANALYZE_PER_BSSID 个 Beacon 帧。
    任意一帧三特征全中 → 立即确认为星链，后续不再分析。
    达到上限仍未全中 → 确认为非星链，后续不再分析。

    这样可以容忍偶发的"异常帧"（如首包 F3 只有 2 条 Vendor），
    避免因第一个包不典型而导致永久误判为非星链。

    线程安全说明：sniff 回调为单线程串行调用，当前实现无需加锁。
    若未来引入多线程 sniff 合并场景，需在 analyze_beacon 外加锁。
    """

    def __init__(self):
        # {bssid: {'is_starlink': bool, 'confirmed': bool,
        #          'analyze_count': int, 'features': list,
        #          'ssid': str, 'channel': str}}
        self._cache: dict = {}

    def analyze_beacon(self, pkt, bssid: str,
                       ssid: str = '', channel: str = '?') -> bool:
        """
        分析一个 Beacon 帧，判断是否为星链设备。

        - 已确认（is_starlink=True 或 analyze_count 达上限）→ 直接返回缓存结果
        - 未确认且未达上限 → 继续分析本帧，三特征全中则立即确认

        参数:
            pkt:     Scapy 解析后的数据包
            bssid:   该 Beacon 帧的 BSSID（字符串）
            ssid:    已解码的 SSID（可选，仅用于日志）
            channel: AP 自声明的信道（来自 DS Parameter Set IE，可选）

        返回:
            is_starlink (bool)
        """
        bssid = bssid.lower().strip()
        entry = self._cache.get(bssid)

        if entry:
            # 已确认为星链，直接返回
            if entry['is_starlink']:
                return True
            # 已达分析上限且未命中，停止重复分析
            if entry['analyze_count'] >= _MAX_ANALYZE_PER_BSSID:
                return False

        try:
            f1 = _check_f1_tpc(pkt)
            f2 = _check_f2_wmm(pkt)
            f3 = _check_f3_vendor(pkt)
        except Exception as e:
            logger.debug(f"[S9] Beacon 解析异常 bssid={bssid}: {e}")
            if entry:
                entry['analyze_count'] += 1
            else:
                self._cache[bssid] = {
                    'is_starlink':   False,
                    'analyze_count': 1,
                    'features':      [],
                    'ssid':          ssid,
                    'channel':       channel,
                }
            return False

        is_starlink = f1 and f2 and f3
        features = []
        if f1: features.append('F1-TPC')
        if f2: features.append('F2-WMM')
        if f3: features.append('F3-Vendor')

        if entry is None:
            self._cache[bssid] = {
                'is_starlink':   is_starlink,
                'analyze_count': 1,
                'features':      features,
                'ssid':          ssid,
                'channel':       channel,
            }
        else:
            entry['analyze_count'] += 1
            if is_starlink:
                # 本帧全中，升级为已确认
                entry['is_starlink'] = True
                entry['features']    = features
                entry['ssid']        = ssid
                entry['channel']     = channel

        if is_starlink:
            count = self._cache[bssid]['analyze_count']
            logger.info(
                f"🛰️ [S9] 发现星链设备: bssid={bssid} "
                f"ssid={ssid!r} ch={channel} 命中={features} "
                f"(第{count}帧)"
            )

        return is_starlink

    def is_starlink(self, bssid: str) -> bool:
        """查询某 BSSID 是否已被识别为星链（未分析过则返回 False）"""
        if not bssid:
            return False
        return self._cache.get(bssid.lower().strip(), {}).get('is_starlink', False)

    def get_all_starlink(self) -> dict:
        """返回所有已确认为星链的设备 {bssid: info}"""
        return {k: v for k, v in self._cache.items() if v['is_starlink']}

    def reset(self):
        """清空缓存，跨项目/跨扫描任务重置时调用"""
        self._cache.clear()
        logger.info("[S9] StarLinkDetector 缓存已清空")

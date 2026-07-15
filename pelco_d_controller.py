#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PelcoD云台控制器
提供完整的PelcoD协议云台控制功能
兼容您项目的PtzControl接口
"""

import struct
import time
import serial
from typing import Tuple, Optional


class PtzControl:
    """PelcoD云台控制器类（兼容您项目的PtzControl接口）"""
    
    def __init__(self, device_address: int = 1, serial_port: str = '/dev/ttyUSB0', 
                 baudrate: int = 9600, timeout: float = 1.0, verbose: bool = False):
        """
        初始化云台控制器
        
        Args:
            device_address: 云台设备地址，默认为1
            serial_port: 串口设备路径，默认/dev/ttyUSB0
            baudrate: 串口波特率，默认9600
            timeout: 串口超时时间，默认1秒
        """
        self.device_address = device_address
        self.sync_byte = 0xFF
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial_conn = None
        self.verbose = verbose
        self._last_position = (None, None)  # 缓存最后获取的位置
        
    def connect(self) -> bool:
        """
        连接串口
        
        Returns:
            连接是否成功
        """
        try:
            self.serial_conn = serial.Serial(
                port=self.serial_port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            return True
        except Exception as e:
            print(f"串口连接失败: {e}")
            return False
    
    def disconnect(self):
        """断开串口连接"""
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
    
    def _send_command(self, command: bytes) -> bool:
        """
        发送指令到串口
        
        Args:
            command: 要发送的指令字节
            
        Returns:
            发送是否成功
        """
        if not self.serial_conn or not self.serial_conn.is_open:
            return False
        
        try:
            self.serial_conn.write(command)
            self.serial_conn.flush()
            return True
        except Exception as e:
            print(f"发送指令失败: {e}")
            return False
    
    def _read_response(self, expected_length: int = 7, timeout: float = 1.0) -> Optional[bytes]:
        """
        读取串口响应
        
        Args:
            expected_length: 期望的响应长度
            timeout: 读取超时时间（秒）
            
        Returns:
            响应数据字节，失败返回None
        """
        if not self.serial_conn or not self.serial_conn.is_open:
            return None
        
        try:
            # 设置读取超时（临时覆盖）
            original_timeout = self.serial_conn.timeout
            self.serial_conn.timeout = 0.05  # 短轮询

            deadline = time.time() + timeout
            buffer = bytearray()

            def try_extract_frame(buf: bytearray) -> Optional[bytes]:
                # Pelco-D 响应帧固定为7字节: FF, addr, 00, cmd, data1, data2, sum
                i = 0
                while i <= len(buf) - expected_length:
                    if buf[i] != 0xFF:
                        i += 1
                        continue
                    frame = buf[i:i + expected_length]
                    # 基本校验: 长度和校验和
                    if len(frame) == expected_length:
                        checksum = sum(frame[1:6]) & 0xFF
                        if checksum == frame[6]:
                            return bytes(frame)
                    i += 1
                return None

            while time.time() < deadline:
                try:
                    chunk = self.serial_conn.read(64)
                except Exception:
                    chunk = b""
                if chunk:
                    buffer.extend(chunk)
                    # 限制缓冲区大小，避免无限增长
                    if len(buffer) > 512:
                        buffer = buffer[-512:]
                    frame = try_extract_frame(buffer)
                    if frame is not None:
                        # 恢复原始超时设置
                        self.serial_conn.timeout = original_timeout
                        return frame
                else:
                    # 无数据，稍作等待
                    time.sleep(0.01)

            # 恢复原始超时设置
            self.serial_conn.timeout = original_timeout

            # 超时未取到完整帧，输出缓冲便于诊断
            if buffer:
                try:
                    print(f"警告: 超时未取到完整帧，缓冲区({len(buffer)}B): {bytes(buffer).hex()}")
                except Exception:
                    pass
            return None

        except Exception as e:
            print(f"读取响应失败: {e}")
            return None
    
    def _calculate_checksum(self, data_bytes: list) -> int:
        """
        计算校验和（除0xFF外其余字节和的低八位）
        
        Args:
            data_bytes: 数据字节列表
            
        Returns:
            校验和
        """
        checksum = sum(data_bytes) & 0xFF
        return checksum
    
    def _build_command(self, cmd1: int, cmd2: int, data1: int = 0, data2: int = 0) -> bytes:
        """
        构建完整的PelcoD指令
        
        Args:
            cmd1: 命令字符1
            cmd2: 命令字符2
            data1: 数据字符1
            data2: 数据字符2
            
        Returns:
            完整的指令字节
        """
        command = [self.sync_byte, self.device_address, cmd1, cmd2, data1, data2]
        checksum = self._calculate_checksum(command[1:])  # 除0xFF外计算校验和
        command.append(checksum)
        return bytes(command)
    
    # ==================== 兼容您项目的接口方法 ====================
    
    def move_to_pan_tilt(self, pan_angle: float, tilt_angle: float) -> bool:
        """
        移动到指定的水平和垂直角度（兼容您项目的接口）
        
        Args:
            pan_angle: 目标水平角度 (0-360度)
            tilt_angle: 目标垂直角度 (-90到+90度)
            
        Returns:
            移动指令是否发送成功
        """
        try:
            # 发送水平角度移动指令
            h_cmd = self.move_to_horizontal_angle(pan_angle)
            if not self._send_command(h_cmd):
                return False
            
            # 发送垂直角度移动指令
            v_cmd = self.move_to_vertical_angle(tilt_angle)
            if not self._send_command(v_cmd):
                return False
            
            return True
        except Exception as e:
            print(f"移动到指定位置失败: {e}")
            return False
    
    def get_position(self) -> Tuple[Optional[float], Optional[float]]:
        """
        获取当前水平和垂直位置（兼容您项目的接口）
        
        Returns:
            (水平角度, 垂直角度) 的元组，失败返回 (None, None)
        """
        try:
            if self.verbose:
                print("🔍 开始查询云台位置...")
            
            # 查询水平位置
            h_query = self.query_horizontal_position()
            if self.verbose:
                print(f"📤 发送水平位置查询命令: {h_query.hex()}")
            
            if not self._send_command(h_query):
                print("❌ 水平位置查询命令发送失败")
                return (None, None)
            
            # 等待响应
            if self.verbose:
                print("⏳ 等待水平位置响应...")
            time.sleep(0.1)  # 增加等待时间
            h_response = self._read_response(timeout=2.0)
            
            if not h_response:
                if self.verbose:
                    print("❌ 未收到水平位置响应")
                # 尝试返回缓存的位置
                if self._last_position and self._last_position != (None, None):
                    if self.verbose:
                        print(f"📋 使用缓存位置: {self._last_position}")
                    return self._last_position
                return (None, None)
            
            if self.verbose:
                print(f"📥 收到水平位置响应: {h_response.hex()}")
            
            # 查询垂直位置
            v_query = self.query_vertical_position()
            if self.verbose:
                print(f"📤 发送垂直位置查询命令: {v_query.hex()}")
            
            if not self._send_command(v_query):
                print("❌ 垂直位置查询命令发送失败")
                return (None, None)
            
            # 等待响应
            if self.verbose:
                print("⏳ 等待垂直位置响应...")
            time.sleep(0.1)  # 增加等待时间
            v_response = self._read_response(timeout=2.0)
            
            if not v_response:
                if self.verbose:
                    print("❌ 未收到垂直位置响应")
                return (None, None)
            
            if self.verbose:
                print(f"📥 收到垂直位置响应: {v_response.hex()}")
            
            # 解析位置数据
            pan = self.parse_horizontal_position(h_response)
            tilt = self.parse_vertical_position(v_response)
            
            # 规范化并保留两位小数
            pan = self._normalize_pan(pan)
            tilt = self._clamp_tilt(tilt)
            pan = round(pan, 2)
            tilt = round(tilt, 2)
            
            #print(f"解析结果: 水平={pan}°, 垂直={tilt}°")
            
            # 缓存位置（规范化后的角度）
            self._last_position = (pan, tilt)
            return (pan, tilt)
            
        except Exception as e:
            print(f"❌ 获取位置失败: {e}")
            import traceback
            traceback.print_exc()
            # 返回缓存的位置或默认值
            return self._last_position if self._last_position != (None, None) else (None, None)

    # ==================== 角度规范化辅助 ====================
    def _normalize_pan(self, angle: float) -> float:
        try:
            a = float(angle) % 360.0
            if a < 0:
                a += 360.0
            return a
        except Exception:
            return angle

    def _clamp_tilt(self, angle: float) -> float:
        try:
            a = float(angle)
            if a > 90.0:
                return 90.0
            if a < -90.0:
                return -90.0
            return a
        except Exception:
            return angle

    def get_position_dict(self, ndigits: int = 2) -> dict:
        """
        获取规范化位置并返回字典，包含字符串格式，便于直接写入Redis/前端展示。
        返回示例：{"pan": 180.0, "tilt": 0.0, "pan_str": "180.00°", "tilt_str": "0.00°"}
        """
        pan, tilt = self.get_position()
        if pan is None or tilt is None:
            return {"pan": None, "tilt": None, "pan_str": None, "tilt_str": None}
        pan_r = round(pan, ndigits)
        tilt_r = round(tilt, ndigits)
        return {
            "pan": pan_r,
            "tilt": tilt_r,
            "pan_str": f"{pan_r:.{ndigits}f}°",
            "tilt_str": f"{tilt_r:.{ndigits}f}°",
        }
    
    def stop(self) -> bool:
        """
        停止所有运动（兼容您项目的接口）
        
        Returns:
            停止指令是否发送成功
        """
        stop_cmd = self._build_command(0x00, 0x00, 0x00, 0x00)
        return self._send_command(stop_cmd)
    
    def move(self, direction: str, offset: float) -> bool:
        """
        相对移动（兼容您项目的接口）
        
        Args:
            direction: 移动方向 ('up', 'down', 'left', 'right')
            offset: 移动角度偏移量（度），由前端输入决定
            
        Returns:
            移动指令是否发送成功
        """
        try:
            # 获取当前位置
            current_pan, current_tilt = self.get_position()
            if current_pan is None or current_tilt is None:
                return False
            
            # 根据方向计算目标角度
            if direction.lower() == 'up':
                target_pan, target_tilt = current_pan, current_tilt + offset
            elif direction.lower() == 'down':
                target_pan, target_tilt = current_pan, current_tilt - offset
            elif direction.lower() == 'left':
                target_pan, target_tilt = current_pan - offset, current_tilt
            elif direction.lower() == 'right':
                target_pan, target_tilt = current_pan + offset, current_tilt
            else:
                print(f"无效的移动方向: {direction}")
                return False
            
            # 处理角度范围
            target_pan = target_pan % 360.0  # 水平角度环绕
            target_tilt = max(-90.0, min(90.0, target_tilt))  # 垂直角度限制
            
            # 使用绝对角度移动
            return self.move_to_pan_tilt(target_pan, target_tilt)
            
        except Exception as e:
            print(f"相对移动失败: {e}")
            return False
    
    # ==================== 原有PelcoD方法（保持兼容性） ====================
    
    def move_up(self, speed: int = 0x20) -> bytes:
        """
        向上移动
        
        Args:
            speed: 移动速度等级 (0x00-0x3F)，默认0x20
            
        Returns:
            向上移动指令字节
        """
        if not 0x00 <= speed <= 0x3F:
            raise ValueError("速度等级必须在0x00-0x3F范围内")
        return self._build_command(0x00, 0x08, 0x00, speed)
    
    def move_down(self, speed: int = 0x20) -> bytes:
        """
        向下移动
        
        Args:
            speed: 移动速度等级 (0x00-0x3F)，默认0x20
            
        Returns:
            向下移动指令字节
        """
        if not 0x00 <= speed <= 0x3F:
            raise ValueError("速度等级必须在0x00-0x3F范围内")
        return self._build_command(0x00, 0x10, 0x00, speed)
    
    def move_left(self, speed: int = 0x20) -> bytes:
        """
        向左移动
        
        Args:
            speed: 移动速度等级 (0x00-0x3F)，默认0x20
            
        Returns:
            向左移动指令字节
        """
        if not 0x00 <= speed <= 0x3F:
            raise ValueError("速度等级必须在0x00-0x3F范围内")
        return self._build_command(0x00, 0x04, speed, 0x00)
    
    def move_right(self, speed: int = 0x20) -> bytes:
        """
        向右移动
        
        Args:
            speed: 移动速度等级 (0x00-0x3F)，默认0x20
            
        Returns:
            向右移动指令字节
        """
        if not 0x00 <= speed <= 0x3F:
            raise ValueError("速度等级必须在0x00-0x3F范围内")
        return self._build_command(0x00, 0x02, speed, 0x00)
    
    # ==================== 扩展功能指令 ====================
    
    def set_preset(self, preset_number: int) -> bytes:
        """
        设置预置位
        
        Args:
            preset_number: 预置位编号
            
        Returns:
            设置预置位指令字节
        """
        if not 0 <= preset_number <= 255:
            raise ValueError("预置位编号必须在0-255范围内")
        return self._build_command(0x00, 0x03, 0x00, preset_number)
    
    def call_preset(self, preset_number: int) -> bytes:
        """
        调用预置位
        
        Args:
            preset_number: 预置位编号
            
        Returns:
            调用预置位指令字节
        """
        if not 0 <= preset_number <= 255:
            raise ValueError("预置位编号必须在0-255范围内")
        return self._build_command(0x00, 0x07, 0x00, preset_number)
    
    def delete_preset(self, preset_number: int) -> bytes:
        """
        删除预置位
        
        Args:
            preset_number: 预置位编号
            
        Returns:
            删除预置位指令字节
        """
        if not 0 <= preset_number <= 255:
            raise ValueError("预置位编号必须在0-255范围内")
        return self._build_command(0x00, 0x05, 0x00, preset_number)
    
    def auxiliary_on(self, aux_number: int) -> bytes:
        """
        开启辅助开关
        
        Args:
            aux_number: 辅助开关编号
            
        Returns:
            开启辅助开关指令字节
        """
        if not 0 <= aux_number <= 255:
            raise ValueError("辅助开关编号必须在0-255范围内")
        return self._build_command(0x00, 0x09, 0x00, aux_number)
    
    def auxiliary_off(self, aux_number: int) -> bytes:
        """
        关闭辅助开关
        
        Args:
            aux_number: 辅助开关编号
            
        Returns:
            关闭辅助开关指令字节
        """
        if not 0 <= aux_number <= 255:
            raise ValueError("辅助开关编号必须在0-255范围内")
        return self._build_command(0x00, 0x0B, 0x00, aux_number)
    
    def restart(self) -> bytes:
        """
        远端重启云台
        
        Returns:
            重启指令字节
        """
        return self._build_command(0x00, 0x0F, 0x00, 0x00)
    
    # ==================== 位置查询指令 ====================
    
    def query_horizontal_position(self) -> bytes:
        """
        查询水平位置
        
        Returns:
            水平位置查询指令字节
        """
        return self._build_command(0x00, 0x51, 0x00, 0x00)
    
    def query_vertical_position(self) -> bytes:
        """
        查询垂直位置
        
        Returns:
            垂直位置查询指令字节
        """
        return self._build_command(0x00, 0x53, 0x00, 0x00)
    
    # ==================== 绝对角度控制指令 ====================
    
    def move_to_horizontal_angle(self, angle: float) -> bytes:
        """
        移动到指定水平角度
        
        Args:
            angle: 目标水平角度 (0-360度)
            
        Returns:
            水平绝对角度控制指令字节
        """
        if not 0 <= angle <= 360:
            raise ValueError("水平角度必须在0-360度范围内")
        
        # 计算角度数据：(DATA1<<8) + DATA2 = 角度*100
        angle_data = int(angle * 100)
        data1 = (angle_data >> 8) & 0xFF
        data2 = angle_data & 0xFF
        
        return self._build_command(0x00, 0x4B, data1, data2)
    
    def move_to_vertical_angle(self, angle: float) -> bytes:
        """
        移动到指定垂直角度
        
        Args:
            angle: 目标垂直角度 (-90到+90度)
            
        Returns:
            垂直绝对角度控制指令字节
        """
        if not -90 <= angle <= 90:
            raise ValueError("垂直角度必须在-90到+90度范围内")
        
        # 计算角度数据
        if angle < 0:
            # 负角度：(DATA1<<8) + DATA2 = 角度*100
            angle_data = int(abs(angle) * 100)
        else:
            # 正角度：(DATA1<<8) + DATA2 = 36000-角度*100
            angle_data = 36000 - int(angle * 100)
        
        data1 = (angle_data >> 8) & 0xFF
        data2 = angle_data & 0xFF
        
        return self._build_command(0x00, 0x4D, data1, data2)
    
    # ==================== 位置解析方法 ====================
    
    def parse_horizontal_position(self, response: bytes) -> float:
        """
        解析水平位置返回指令
        
        Args:
            response: 云台返回的水平位置指令字节
            
        Returns:
            水平角度值
        """
        if len(response) != 7 or response[0] != 0xFF:
            raise ValueError("无效的水平位置返回指令")
        
        # 指令格式：FF 01 00 59 PMSB PLSB SUM
        if response[2] != 0x00 or response[3] != 0x59:
            raise ValueError("不是水平位置返回指令")
        
        pmsb = response[4]
        plsb = response[5]
        
        # 计算角度：Pdata = PMSB*256 + PLSB, Pangle = Pdata/100
        pdata = pmsb * 256 + plsb
        pangle = pdata / 100.0
        
        return pangle
    
    def parse_vertical_position(self, response: bytes) -> float:
        """
        解析垂直位置返回指令
        
        Args:
            response: 云台返回的垂直位置指令字节
            
        Returns:
            垂直角度值
        """
        if len(response) != 7 or response[0] != 0xFF:
            raise ValueError("无效的垂直位置返回指令")
        
        # 指令格式：FF 01 00 5B TMSB TLSB SUM
        if response[2] != 0x00 or response[3] != 0x5B:
            raise ValueError("不是垂直位置返回指令")
        
        tmsb = response[4]
        tlsb = response[5]
        
        # 计算角度
        tdata1 = tmsb * 256 + tlsb
        
        if tdata1 > 18000:
            tdata2 = 36000 - tdata1
        else:
            tdata2 = -tdata1
        
        tangle = tdata2 / 100.0
        
        return tangle
    
    # ==================== 便捷控制方法 ====================
    
    def move_diagonal(self, horizontal_direction: str, vertical_direction: str, 
                     horizontal_speed: int = 0x20, vertical_speed: int = 0x20) -> Tuple[bytes, bytes]:
        """
        对角线移动（同时水平和垂直移动）
        
        Args:
            horizontal_direction: 水平方向 ('left' 或 'right')
            vertical_direction: 垂直方向 ('up' 或 'down')
            horizontal_speed: 水平移动速度
            vertical_speed: 垂直移动速度
            
        Returns:
            (水平移动指令, 垂直移动指令) 的元组
        """
        horizontal_cmd = None
        vertical_cmd = None
        
        if horizontal_direction.lower() == 'left':
            horizontal_cmd = self.move_left(horizontal_speed)
        elif horizontal_direction.lower() == 'right':
            horizontal_cmd = self.move_right(horizontal_speed)
        
        if vertical_direction.lower() == 'up':
            vertical_cmd = self.move_up(vertical_speed)
        elif vertical_direction.lower() == 'down':
            vertical_cmd = self.move_down(vertical_speed)
        
        return horizontal_cmd, vertical_cmd
    
    def set_speed_level(self, speed: int) -> int:
        """
        设置速度等级（标准化到0x00-0x3F范围）
        
        Args:
            speed: 原始速度值
            
        Returns:
            标准化后的速度等级
        """
        if speed < 0:
            return 0x00
        elif speed > 63:
            return 0x3F
        else:
            return speed
    
    def get_command_info(self, command: bytes) -> dict:
        """
        获取指令的详细信息
        
        Args:
            command: 指令字节
            
        Returns:
            包含指令信息的字典
        """
        if len(command) != 7:
            return {"error": "无效的指令长度"}
        
        info = {
            "sync_byte": f"0x{command[0]:02X}",
            "address": f"0x{command[1]:02X}",
            "cmd1": f"0x{command[2]:02X}",
            "cmd2": f"0x{command[3]:02X}",
            "data1": f"0x{command[4]:02X}",
            "data2": f"0x{command[5]:02X}",
            "checksum": f"0x{command[6]:02X}",
            "raw_bytes": [f"0x{b:02X}" for b in command]
        }
        
        return info


# ==================== 使用示例 ====================

if __name__ == "__main__":
    # 创建控制器实例（云台地址为1）
    controller = PtzControl(device_address=1)
    
    # 测试兼容接口
    print("=== 兼容接口测试 ===")
    print("1. 移动到指定位置")
    success = controller.move_to_pan_tilt(90.0, 0.0)
    print(f"   移动到(90°, 0°): {'成功' if success else '失败'}")
    
    print("2. 获取当前位置")
    pan, tilt = controller.get_position()
    print(f"   当前位置: 水平={pan}°, 垂直={tilt}°")
    
    print("3. 停止移动")
    success = controller.stop()
    print(f"   停止移动: {'成功' if success else '失败'}")
    
    # 测试原有PelcoD方法
    print("\n=== 原有PelcoD方法测试 ===")
    print(f"向上移动: {controller.move_up(0x30).hex()}")
    print(f"向右移动: {controller.move_right(0x25).hex()}")
    print(f"停止移动: {controller.stop().hex()}")
    
    print("\n=== 完全无缝兼容说明 ===")
print("现在您可以直接使用以下方法，无需修改任何现有代码：")
print("- ptz.move_to_pan_tilt(pan, tilt)       # 绝对角度移动")
print("- ptz.get_position()                     # 获取当前位置")
print("- ptz.stop()                             # 停止移动")
print("- ptz.move(direction, offset)            # 相对移动（offset为角度偏移量）")
print("\n✅ 完全兼容您现有的 PtzControl 接口！")

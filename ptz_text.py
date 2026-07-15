#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云台精度测试程序 - 简化版
专门用于90°-270°范围内5°步长的小步径测试
"""

import time
from datetime import datetime
from pelco_d_controller import PtzControl


class SimplePtzTester:
    """简化版云台测试器"""
    
    def __init__(self, device_address=1, serial_port='/dev/ttyUSB0', baudrate=9600):
        """初始化测试器"""
        self.ptz = PtzControl(
            device_address=device_address,
            serial_port=serial_port,
            baudrate=baudrate,
            timeout=2.0,
            verbose=True
        )
        
        # 测试参数
        self.pan_min = 90.0      # 起始角度
        self.pan_max = 270.0     # 结束角度
        self.tilt = 0.0          # 垂直角度（固定）
        self.step = 5.0          # 步长
        self.step_delay = 0.5    # 每步之间停顿时间
        
        # 统计
        self.cycle_count = 0
        self.move_count = 0
    
    def connect(self):
        """连接云台"""
        print("🔗 连接云台...")
        if self.ptz.connect():
            print("✅ 连接成功")
            return True
        else:
            print("❌ 连接失败")
            return False
    
    def move_and_wait(self, target_pan):
        """移动到指定位置并等待到位"""
        print(f"🎯 移动到 {target_pan:.0f}°")
        
        # 发送移动指令
        success = self.ptz.move_to_pan_tilt(target_pan, self.tilt)
        if not success:
            print("   ❌ 指令发送失败")
            return False
        
        # 等待到位（简化版：固定等待时间）
        time.sleep(2.0)  # 等待云台移动
        
        # 查询实际位置
        actual_pan, actual_tilt = self.ptz.get_position()
        if actual_pan is not None:
            error = abs(actual_pan - target_pan)
            if error > 180:  # 处理角度环绕
                error = 360 - error
            print(f"   📍 实际位置: {actual_pan:.1f}°, 误差: {error:.1f}°")
        else:
            print("   ❌ 位置查询失败")
        
        self.move_count += 1
        return True
    
    def run_cycle(self):
        """运行一个完整循环"""
        self.cycle_count += 1
        print(f"\n{'='*50}")
        print(f"🔄 开始第 {self.cycle_count} 个循环")
        print(f"{'='*50}")
        
        # 正向移动：90° → 270°
        print("📈 正向移动...")
        current_pan = self.pan_min
        while current_pan <= self.pan_max:
            if self.move_and_wait(current_pan):
                time.sleep(self.step_delay)  # 每步停顿
            current_pan += self.step
        
        print("⏸️  正向完成，停顿1秒")
        time.sleep(1.0)
        
        # 反向移动：270° → 90°
        print("📉 反向移动...")
        current_pan = self.pan_max - self.step  # 避免重复270°
        while current_pan >= self.pan_min:
            if self.move_and_wait(current_pan):
                time.sleep(self.step_delay)  # 每步停顿
            current_pan -= self.step
        
        print(f"✅ 循环 {self.cycle_count} 完成")
    
    def run_test(self):
        """运行持续测试"""
        print("🎯 云台小步径测试 - 简化版")
        print(f"📋 测试范围: {self.pan_min}° - {self.pan_max}°")
        print(f"📏 步长: {self.step}°")
        print(f"⏱️  每步停顿: {self.step_delay}秒")
        print(f"📐 垂直角度: {self.tilt}°")
        print("💡 按 Ctrl+C 停止测试")
        print("=" * 50)
        
        # 连接云台
        if not self.connect():
            return
        
        try:
            # 持续循环测试
            while True:
                self.run_cycle()
                
                # 循环间休息
                print("😴 休息2秒后继续...")
                time.sleep(2.0)
                
        except KeyboardInterrupt:
            print("\n🛑 用户停止测试")
        except Exception as e:
            print(f"\n❌ 程序异常: {e}")
        finally:
            self.cleanup()
    
    def cleanup(self):
        """清理资源"""
        print(f"\n📊 测试统计:")
        print(f"   总循环数: {self.cycle_count}")
        print(f"   总移动数: {self.move_count}")
        print(f"   结束时间: {datetime.now().strftime('%H:%M:%S')}")
        
        try:
            print("🛑 停止云台...")
            self.ptz.stop()
            print("🔌 断开连接...")
            self.ptz.disconnect()
        except:
            pass
        
        print("👋 测试结束")


def main():
    """主函数"""
    # 配置参数
    DEVICE_ADDRESS = 1
    SERIAL_PORT = '/dev/ttyUSB0'
    BAUDRATE = 9600
    
    # 创建并运行测试器
    tester = SimplePtzTester(
        device_address=DEVICE_ADDRESS,
        serial_port=SERIAL_PORT,
        baudrate=BAUDRATE
    )
    
    tester.run_test()


if __name__ == "__main__":
    main()
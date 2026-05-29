#!/bin/bash
# 校园网自动认证脚本启动器

echo "正在启动校园网自动认证脚本..."

# 切换到脚本目录
cd "$(dirname "$0")"

# 检查Python是否安装
if ! command -v python &> /dev/null; then
    echo "错误: 未找到Python，请先安装Python"
    exit 1
fi

# 运行Python脚本
python auto_wifi_login.py

echo ""
read -p "按回车键退出..."

#!/bin/bash
# SmartLife Agent 一键启动

cd "$(dirname "$0")"

# 激活虚拟环境
source venv312/bin/activate

# 启动应用
streamlit run app/main.py

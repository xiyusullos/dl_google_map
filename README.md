# 安装 uv

```cmd
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## path 临时生效
```cmd
set Path=%USERPROFILE%\.local\bin;%Path%
```

> 注意: 若要永久生效, 请自行修改环境变量

# 安装Python
```cmd
uv python install 3.12
```

# 启动项目

进入到项目目录下
```cmd
uv sync

uv run main.py
```
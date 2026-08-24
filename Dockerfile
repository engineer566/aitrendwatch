FROM python:3.13-slim

# 设置工作目录
WORKDIR /app

# 设置时区
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY app.py ./
COPY templates ./templates

# 暴露端口
EXPOSE 5000

# gunicorn 生产级 WSGI 启动：4 worker，绑定 0.0.0.0:5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", \
     "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-", \
     "app:app"]

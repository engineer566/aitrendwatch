FROM python:3.13-slim

# 设置工作目录
WORKDIR /app

# 设置时区
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 只装依赖（利用 Docker 层缓存：requirements.txt 不变则此层不重跑）。
# 代码不 COPY 进镜像——compose 用 bind mount 挂载源码（:ro），镜像只负责运行时依赖。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 暴露端口
EXPOSE 5000

# gunicorn 生产级 WSGI 启动：2 worker（1.6G 小内存主机，减 worker 数压稳态内存）。
# max-requests 50000 + jitter 300：回收频率压到 ~33h/次（避免 worker 频繁回收触发
# 启动刷新→高频 LLM 调用），同时仍保留长跑进程偶发清零、防内存爬升。
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", \
     "--timeout", "60", "--max-requests", "50000", "--max-requests-jitter", "300", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "app:app"]

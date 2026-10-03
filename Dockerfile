FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg aria2 util-linux gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 -d /home/bot bot
ENV PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
# يوتيوب محتاج JS runtime لحل التوقيعات
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# sed: لو الملف اتعدّل على Windows (نهايات أسطر CRLF) ما يبوظش الـ entrypoint
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh
# الـ entrypoint بيحدّث المحركات ويجهّز /data ثم يشغّل البوت بمستخدم غير root
ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "bot.py"]

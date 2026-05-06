# First, specify the base Docker image.
# You can see the Docker images from Apify at https://hub.docker.com/r/apify/.
FROM apify/actor-python:3.11

# --- 修改部分：安装 Tesseract 与 Playwright Chromium 系统依赖 ---
USER root

RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libnspr4 \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    # 如果需要处理更多语言（如中文），取消下行注释
    # tesseract-ocr-chi-sim \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
# ---------------------------------------

USER myuser

# Second, copy just requirements.txt into the Actor image
COPY --chown=myuser:myuser requirements.txt ./

# Install the packages specified in requirements.txt
RUN echo "Python version:" \
 && python --version \
 && echo "Pip version:" \
 && pip --version \
 && echo "Installing dependencies:" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "Installing Playwright Chromium for OCR screenshots:" \
 && python -m playwright install chromium \
 && echo "All installed Python packages:" \
 && pip freeze

# Next, copy the remaining files and directories with the source code.
COPY --chown=myuser:myuser . ./

# Use compileall to ensure the runnability of the Actor Python code.
RUN python3 -m compileall -q src/

# Specify how to launch the source code of your Actor.
CMD ["python3", "-m", "src"]

# Playwright's Python image ships headless Chromium and its system deps, which
# is the painful part of running this app elsewhere. Pin to a Playwright version
# that matches requirements.txt (playwright>=1.44).
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Times New Roman, etc. — the resume spec renders in Times New Roman; without the
# msttcorefonts Chromium falls back to a different serif and the PDF metrics shift.
USER root
RUN apt-get update \
    && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        ttf-mscorefonts-installer \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

COPY . .

EXPOSE 5001
CMD ["python", "app.py"]

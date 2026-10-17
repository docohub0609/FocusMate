FROM python:3.13-slim-bookworm

RUN groupadd --gid 10001 focusmate \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin focusmate

USER 10001:10001
WORKDIR /workspace

CMD ["python", "-I", "-B", "/workspace/main.py"]

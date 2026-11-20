FROM python:3.12-slim
RUN pip install --no-cache-dir 'numpy>=1.26,<3' 'pandas>=2.2,<3' 'matplotlib>=3.8,<4' 'pytest>=8,<9'
COPY runner.py /opt/harness_runner.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg MPLCONFIGDIR=/tmp/matplotlib HOME=/tmp
USER 1000:1000
WORKDIR /work
ENTRYPOINT ["python", "-I", "/opt/harness_runner.py"]

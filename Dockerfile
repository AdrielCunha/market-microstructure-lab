# Minor travado, patch livre. O simulador precisa dar o mesmo número aqui e na
# máquina de desenvolvimento — resultado que muda porque o runtime mudou não é
# medição —, mas travar o patch também congelaria correção de segurança numa
# máquina que fica meses ligada. Dev roda 3.14.2; a imagem acompanha o 3.14.x.
FROM python:3.14-slim

# Camada de dependências separada do código: alterar uma linha de Python não
# reinstala polars e pyarrow, que são as pesadas. Na droplet de 1 vCPU isso é a
# diferença entre um deploy de 20 segundos e um de 3 minutos.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Usuário sem privilégio. O diretório `data` é montado do host e precisa
# pertencer a este UID — o setup da VPS faz o chown.
RUN useradd --uid 1000 --create-home pmlab && chown -R pmlab:pmlab /app
USER pmlab

# Sem buffer: com stdout em buffer, o log do coletor só aparece em blocos e
# fica impossível acompanhar `docker logs -f` durante uma falha.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

EXPOSE 8787

# O painel respondendo é o sinal de vida honesto: já aconteceu de o processo
# ficar de pé, com a porta escutando, e nada mais acontecer. O watchdog interno
# derruba o processo nesse caso e a política de restart sobe de novo.
HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8787/',timeout=10).status_code==200 else 1)"

CMD ["python", "-m", "collector.run"]

# syntax=docker/dockerfile:1
# Compose supplies the collector service as a named build context.
FROM collector
ARG TARGETARCH

ENV DISPLAY=:99 \
    PROMOTION_LOGIN_MODE=remote \
    PROMOTION_BROWSER_CHANNEL=chromium \
    PROMOTION_PANEL_ORIGIN=http://127.0.0.1:18762 \
    PROMOTION_AUTH_FILE=/run/promotion/authorization/admin.htpasswd

RUN --mount=type=cache,id=promotion-apt-${TARGETARCH},target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=promotion-apt-lists-${TARGETARCH},target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       xvfb x11-utils openbox x11vnc novnc websockify nginx-light apache2-utils \
    && rm -f /etc/nginx/sites-enabled/default

COPY docker/login /app/docker/login
COPY scripts/init_authorization.py /app/scripts/init_authorization.py
RUN chmod 755 /app/docker/login/entrypoint.sh

EXPOSE 18762
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/docker/login/healthcheck.py"]
ENTRYPOINT ["/app/docker/login/entrypoint.sh"]
CMD []

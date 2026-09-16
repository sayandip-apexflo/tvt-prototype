FROM nginx:1.28-alpine
COPY deploy/ui/nginx.conf /etc/nginx/nginx.conf
COPY deploy/ui/web/dist/ /usr/share/nginx/html/
EXPOSE 18081
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:18081/healthz || exit 1

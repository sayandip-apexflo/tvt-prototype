FROM nginx:1.28-alpine
COPY deploy/ui/nginx.conf /etc/nginx/nginx.conf
COPY apexfabric/control_plane/static/site.html /usr/share/nginx/html/index.html
COPY apexfabric/control_plane/static/site.js /usr/share/nginx/html/site.js
COPY apexfabric/control_plane/static/site.css /usr/share/nginx/html/site.css
COPY apexfabric/control_plane/static/customer.html /usr/share/nginx/html/customer.html
COPY apexfabric/control_plane/static/customer.js /usr/share/nginx/html/customer.js
COPY apexfabric/control_plane/static/alerts.js /usr/share/nginx/html/alerts.js
EXPOSE 18081
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:18081/healthz || exit 1

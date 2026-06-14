FROM nginx:stable-alpine

# Copy the static frontend files
COPY frontend /usr/share/nginx/html

# Replace default nginx conf to proxy API requests to the backend service
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

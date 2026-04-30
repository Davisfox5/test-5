# syntax=docker/dockerfile:1.7
#
# Linda marketing site + interactive demo. Static HTML/CSS/JS served
# by nginx — no build step, no runtime dependencies beyond nginx
# itself. Both pages link out to the SPA at https://linda-staging-app.fly.dev/
# for sign-up and authenticated flows.

FROM nginx:1.27-alpine

RUN rm -rf /usr/share/nginx/html/*

COPY index.html demo.html /usr/share/nginx/html/
COPY css/ /usr/share/nginx/html/css/
COPY js/  /usr/share/nginx/html/js/

COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]

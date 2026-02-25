FROM denoland/deno:alpine-1.44.0

WORKDIR /app

COPY server.ts /app/server.ts
COPY public/ /app/public/

ENV PORT=8000
EXPOSE 8000

CMD ["run", "--allow-net", "--allow-read", "--allow-env", "/app/server.ts"]

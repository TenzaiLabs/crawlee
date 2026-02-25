FROM node:20-alpine

WORKDIR /app

COPY package.json /app/package.json
RUN npm install --production

COPY server.js /app/server.js

ENV PORT=8000
EXPOSE 8000

CMD ["node", "server.js"]

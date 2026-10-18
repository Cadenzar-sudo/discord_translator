FROM node:22-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY index.js .

# same mounted config.json the Python server reads
ENV CONFIG_PATH=/data/config.json
ENV NODE_ENV=production

USER node
CMD ["node", "index.js"]

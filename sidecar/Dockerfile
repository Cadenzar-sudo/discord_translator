# build stage: @discordjs/opus has no Node-22 prebuild for this ABI/glibc,
# so compile it (and anything else native) with a toolchain present
FROM node:22-slim AS deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM node:22-slim
WORKDIR /app
COPY --from=deps /app/node_modules node_modules
COPY index.js .

# same mounted config.json the Python server reads
ENV CONFIG_PATH=/data/config.json
ENV NODE_ENV=production

USER node
CMD ["node", "index.js"]

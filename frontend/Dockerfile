# Patch-pinned base (not the mutable node:20-alpine) for reproducible builds.
FROM node:20.18.0-alpine
WORKDIR /app

# npm ci (not npm install): installs exactly what the committed lockfile pins —
# a mutable install can silently drift the dependency tree.
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build

# Least privilege: the Next.js standalone server needs no root. The node image
# ships a dedicated 'node' user; hand over /app so the server can write its
# build cache at runtime.
RUN chown -R node:node /app
USER node

EXPOSE 3000
CMD ["npm", "start"]

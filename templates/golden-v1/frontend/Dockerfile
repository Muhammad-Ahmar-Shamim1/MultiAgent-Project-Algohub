FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# NEXT_PUBLIC_* vars are inlined into the client bundle at build time, not read at container
# start - so this has to be a build arg, not just a runtime `environment:` entry in compose.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

FROM node:20-slim
ENV NODE_ENV=production
WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home /app app

COPY --from=build --chown=app:app /app/public ./public
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static

USER app

EXPOSE 3000
CMD ["node", "server.js"]

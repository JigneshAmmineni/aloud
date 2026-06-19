# Production frontend image: compile the Next.js app, then serve it.
# (Dev uses the node image + `npm run dev` live; this is the real build.)

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-alpine
WORKDIR /app
# NODE_ENV=production also strips the dev-only window.__aloudClient hook
ENV NODE_ENV=production
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "start"]          # next start, serves the compiled app on :3000

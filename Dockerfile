FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production --ignore-optional
COPY . .
CMD ["node", "index.js"]

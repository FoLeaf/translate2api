FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
COPY web ./web
RUN go mod tidy && CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /out/transhub .

FROM alpine:3.20
RUN apk add --no-cache chromium nss freetype harfbuzz ca-certificates tzdata
ENV TH_CHROMIUM_BIN=/usr/bin/chromium-browser
COPY --from=build /out/transhub /usr/local/bin/transhub
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD wget -q -O /dev/null http://127.0.0.1:8000/healthz || exit 1
ENTRYPOINT ["transhub"]

FROM golang:1.22-alpine AS builder

WORKDIR /src
COPY go.mod /src/go.mod
COPY main.go /src/main.go
COPY templates/ /src/templates/

RUN go build -o /bin/complex-auth /src/main.go

FROM alpine:3.20
WORKDIR /app
COPY --from=builder /bin/complex-auth /app/complex-auth
COPY templates/ /app/templates/

EXPOSE 8000

CMD ["/app/complex-auth"]

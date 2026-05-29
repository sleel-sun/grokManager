FROM alpine:3.22

RUN apk add --no-cache privoxy

EXPOSE 8118

CMD ["privoxy", "--no-daemon", "/etc/privoxy/config"]

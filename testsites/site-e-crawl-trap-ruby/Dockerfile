FROM ruby:3.3-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY Gemfile /app/Gemfile
RUN bundle install

COPY app.rb /app/app.rb
COPY views/ /app/views/

ENV PORT=8000
EXPOSE 8000

CMD ["ruby", "app.rb"]

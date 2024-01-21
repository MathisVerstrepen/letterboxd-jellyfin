FROM python:3.11

RUN apt-get update && apt-get -y install cron vim

WORKDIR /app

COPY crontab /etc/cron.d/crontab
RUN chmod 0644 /etc/cron.d/crontab && /usr/bin/crontab /etc/cron.d/crontab

COPY ./requirements.txt /app/requirements.txt
RUN pip3 install -r requirements.txt --no-cache-dir 

COPY ./params.json /app/params.json
COPY ./discord_template.txt /app/discord_template.txt
COPY main.py /app/main.py
COPY /src/*.py /app/src/
COPY ./.env /app/.env

# run crond as main process of container
CMD ["cron", "-f"]
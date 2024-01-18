# sudo docker build -t letterboxd-jellyfin-cron .
# sudo docker run -it --rm --name letterboxd-jellyfin-cron --env-file .env letterboxd-jellyfin-cron

FROM python:3.11

RUN apt-get update && apt-get -y install cron vim

WORKDIR /app

COPY crontab /etc/cron.d/crontab
RUN chmod 0644 /etc/cron.d/crontab && /usr/bin/crontab /etc/cron.d/crontab

COPY ./requirements.txt /app/requirements.txt
RUN pip3 install -r requirements.txt --no-cache-dir 

COPY ./params.json /app/params.json
COPY main.py /app/main.py
COPY /src/*.py /app/src/

# run crond as main process of container
CMD ["cron", "-f"]
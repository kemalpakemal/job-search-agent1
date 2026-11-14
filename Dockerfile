FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY source.zip /tmp/source.zip
RUN python -c "import zipfile; zipfile.ZipFile('/tmp/source.zip').extractall('/app')" \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && mkdir -p /data
COPY scoring.py /app/scoring.py
COPY connectors.py /app/connectors.py
COPY main.py /app/main.py
COPY telegram_bot.py /app/telegram_bot.py
RUN python -m py_compile /app/main.py /app/connectors.py /app/scoring.py /app/telegram_bot.py \
    && python -c "from scoring import score_vacancy, is_profile_relevant, has_training_offer, has_experience_conflict; exact={'title':'Бухгалтер по зарплате','city':'Сочи'}; unrelated={'title':'Бухгалтер-эксперт','city':'Сочи','description':'Расчёт себестоимости, дебиторская задолженность, акты сверки'}; training={'title':'Ведущий бухгалтер по расчёту заработной платы','city':'Сочи','description':'Расчёт зарплаты, больничные, отпускные. Обучим сами, опыт не требуется'}; assert score_vacancy(exact)[0] >= 80; assert not is_profile_relevant(unrelated); assert is_profile_relevant(training); assert has_training_offer(training['description']); assert not has_experience_conflict('От 5 лет опыта, но обучим сами')"
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

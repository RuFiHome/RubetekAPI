# Rubetek API Library

[![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz)

> Python-библиотека для работы с экосистемой умного дома Rubetek с расширенным функционалом

## 📋 Описание

Библиотека предоставляет удобный интерфейс для подключения к облачной платформе Rubetek, получения данных об устройствах, камерах и мониторинга показаний счётчиков водоснабжения. Основана на [RubetekSocketApi](https://github.com/regenara/rubetek_socket_api)

## ✨ Возможности

- 🔐 Авторизация и управление сессией подключения к Rubetek Cloud
- 📡 Получение списка устройств и их текущего состояния
- 🎥 Работа с камерами: статус, поток, события
- 💧 Мониторинг показаний счётчиков ХВС и ГВС с историей данных
- 🔔 Подписка на события в реальном времени (WebSocket)
- 🔄 Автоматическое переподключение при обрыве связи
- 🪵 Логирование с настраиваемым уровнем детализации

## 🚀 Установка

```bash
# Установка из PyPI (когда будет опубликовано)
pip install ...

# Или установка из исходников
pip install git+https://github.com/RuFiHome/RubetekAPI.git
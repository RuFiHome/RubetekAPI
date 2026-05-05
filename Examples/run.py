import asyncio
from SAARubetekAPI import SAARubetekAPI


from SAARubetekAPI.exceptions import AuthorizationRequiredRubetekAPIError

async def test_api():
    api = SAARubetekAPI(device_id="YRuGDRZE5KfoI7jl")
    try:
        try:
            await api.refresh_tokens()
        except AuthorizationRequiredRubetekAPIError as error:
            print(error)
            phone = input("Введите номер телефона (c +7): ")
            await api.authentication(phone=phone)
            code = input("Введите последние 4 цифры номера звонившего телефона: ")
            await api.authorization(phone=phone, code=code)
        print("Успешно авторизовались. Получим информацию")
        user = await api.get_user()
        print("User=(%s)" % str(user))
        house = await api.get_homes()
        print("Homes=(%s)" % house)
        print("Нам нужен только 1 дом")
        api.house_id = house[0].id if house else None
        print("Установили дом: %s" % api.house_id)
        intercoms = await api.get_intercoms()
        print("INERCOMS=(%s)" % intercoms)
        devices = await api.get_devices()
        print("Devices=(%s)" % devices)
        cameras = await api.get_cameras()
        print("Cameras=(%s)" % cameras)
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(test_api())

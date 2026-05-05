class RubetekAPIError(Exception):
    """"""


class AuthorizationRequiredRubetekAPIError(RubetekAPIError):
    def __init__(self, message):
        super().__init__("\n--------------------------------------------------------------------------\n"
                         f"{message}"
                         "rubetek_api = RubetekAPI()\n"
                         "phone = input('Введите номер телефона (c +7): ')"
                         "await rubetek_api.authentication(phone=phone)"
                         "code = input('Введите последние 4 цифры номера звонившего телефона: ')"
                         "await rubetek_api.authorization(phone=phone, code=code)"
                         "\n--------------------------------------------------------------------------\n")


class ClientConnectorRubetekAPIError(RubetekAPIError):
    """"""


class UnauthorizedRubetekAPIError(RubetekAPIError):
    """"""


class DDosRubetekAPIError(RubetekAPIError):
    """"""


class TimeoutRubetekAPIError(RubetekAPIError):
    """"""


class UnknownRubetekAPIError(RubetekAPIError):
    """"""

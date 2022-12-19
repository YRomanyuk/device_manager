# wb-device-manager

Структура данных в топике списка устройств:

```jsonc
{
    // сервис выполняет сканирование портов
    "scanning": true,

    // прогресс завершения сканирования портов
    "progress": 50,

    // ошибка, не относящаяся к конкретному устройству
    // (например, rpc-timeout при неработающем wb-mqtt-serial)
    "error": {
        // принятый внутри команды идентификатор сообщения (формат строго определён!)
        "title": "com.wb.device_manager.generic_error",
        // fallback человекочитаемое сообщение
        "message": "RPC call to wb-mqtt-serial timed out"
    },

    // список устройств
    "devices" : [
        {
            // название устройства (для людей)
            "title": "MR6C",

            // для обращения к устройству; формируется при первом сканировании устройства
            "uuid": "9b5cbc0a-24b3-3065-b105-c999b0293a97",

            // серийный номер устройства
            "sn": "13453ghh",

            // название устройства (для внутреннего использования)
            "device_signature": "WBMR6C",

            // сигнатура прошивки (для внутреннего использования)
            "fw_signature": "mr6cG",

            // устройство доступно и отвечает на запросы
            "online": true,

            // устройство опрашивается через wb-mqtt-serial
            // в текущей итерации не реализовано со стороны wb-mqtt-serial; всегда true
            "poll": true,

            // unix ts последнего сканирования устройства
            "last_seen": 1668154795454,

            // устройство в режиме загрузчика
            "bootloader_mode": true,

            // последняя ошибка при работе с конкретным устройством
            "error": {
                // принятый внутри команды идентификатор сообщения (формат строго определён!)
                "title": "com.wb.device_manager.device_error",
                // fallback человекочитаемое сообщение
                "message": "Modbus communication failed. Check logs for more info"
            },

            // порт, к которому подключено устройство
            "port": {

                // системный путь до устройства порта
                "path": "/dev/ttyRS485-2"
            },

            // текущие настройки устройства
            "cfg": {
                // адрес
                "slave_id": 100,

                // скорость шины
                "baud_rate": 9600,

                // чётность
                "parity": "N",

                // число бит данных
                "data_bits": 8,

                // число стоп бит
                "stop_bits": 2
            },

            // прошивка устройства
            "fw": {
                // версия
                "version": "1.2.3",

                "update": {
                    // процент завершения процесса обновления прошивки
                    "progress": 50,

                    // последняя ошибка обновления прошивки конкретного устройства
                    "error": {
                        // принятый внутри команды идентификатор сообщения (формат строго определён!)
                        "title": "com.wb.device_manager.fw_update_error",
                        // fallback человекочитаемое сообщение
                        "message": "FW update failed. Check logs for more info"
                    },

                    // Актуальная версия прошивки (для текущего релиза)
                    "available_fw": "2.2.2"
                }
            }
        },
        ...
    ]
}
```


### Работа с ошибками
* ошибки в понятном человеку виде отображаются в webui
* ошибки в webui переведены. Webui смотрит в поле ```error.title``` и ищет для него человекочитаемое сообщение для нужной локали. Если таковое не найдено - показывает то, что в ```error.message```
* поле ```error.title``` имеет строго определённый формат: ```com.wb.название_пакета.тип_ошибки```
* подробные ошибки из питона (со stack trace) доступны в логах (```journalctl -u wb-device-manager -f```)

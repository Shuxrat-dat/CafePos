from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_hub_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='cafesettings',
            name='guest_auto_send',
            field=models.BooleanField(
                default=True,
                help_text='Заказ гостя со стола сразу попадает в работу и на кухню. Выключено — его сначала подтверждает официант.',
                verbose_name='Заказы по QR сразу на кухню',
            ),
        ),
    ]

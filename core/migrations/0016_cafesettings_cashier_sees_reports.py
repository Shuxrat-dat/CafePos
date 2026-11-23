from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_payment_click'),
    ]

    operations = [
        migrations.AddField(
            model_name='cafesettings',
            name='cashier_sees_reports',
            field=models.BooleanField(
                default=False,
                help_text='Кассир видит раздел «Отчёты» целиком: выручку, блюда, расходы, зарплату. Менять там он ничего не может.',
                verbose_name='Показывать кассиру отчёты',
            ),
        ),
    ]

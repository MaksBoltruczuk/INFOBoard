# Generated by Django 3.2.12 on 2022-02-28 10:21

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('lti1p3_tool_config', '0001_initial'),
        ('ltiapi', '0002_auto_20220222_1554'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='registered_via',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='lti1p3_tool_config.ltitool', verbose_name='registered via'),
        ),
    ]

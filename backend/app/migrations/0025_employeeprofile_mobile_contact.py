from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0024_merge_20261121_0226'),
    ]

    operations = [
        migrations.AddField(
            model_name='employeeprofile',
            name='mobile_contact',
            field=models.CharField(blank=True, max_length=30),
        ),
    ]

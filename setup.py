from setuptools import setup


setup(
    name='opsworks-easy-deploy',
    version='1.0.0',
    packages=['easy_deploy'],
    entry_points={
        'console_scripts': [
            'opsworks-easy-deploy = easy_deploy.easy_deploy:main',
        ]
    }
)

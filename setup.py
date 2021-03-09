from setuptools import setup


setup(
    name='opsworks-easy-deploy',
    version='1.2.0',
    packages=['easy_deploy'],
    install_requires=[
        'botocore==1.20.23',
        'click==7.1.2',
        'arrow==1.0.3',
    ],
    entry_points={
        'console_scripts': [
            'opsworks-easy-deploy = easy_deploy.easy_deploy:main',
        ]
    }
)

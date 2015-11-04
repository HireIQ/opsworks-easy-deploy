from setuptools import setup


setup(
    name='opsworks-easy-deploy',
    version='1.1.0',
    packages=['easy_deploy'],
    install_requires=[
        'botocore==1.2.0',
        'click==5.1',
        'arrow==0.6.0',
    ],
    entry_points={
        'console_scripts': [
            'opsworks-easy-deploy = easy_deploy.easy_deploy:main',
        ]
    }
)

from setuptools import setup
from setuptools.command.install import install
from setuptools.command.develop import develop


def _post_install_hint():
    print("\nAdd to PATH or run via: python -m unifi_restart\n")


class PostInstall(install):
    def run(self):
        super().run()
        _post_install_hint()


class PostDevelop(develop):
    def run(self):
        super().run()
        _post_install_hint()


setup(cmdclass={"install": PostInstall, "develop": PostDevelop})

# Zielsystem fuer den Integrationstest: Debian mit sshd, ohne curl/wget/python3.
# Laeuft spaeter in einem Docker-Netz OHNE Internetzugang (--internal).
# Ein aelteres, datiertes Basis-Image sorgt dafuer, dass echte Updates anstehen.
ARG DEBIAN_TAG=bookworm-20240513
FROM debian:${DEBIAN_TAG}
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server sudo \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
 && mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh \
 && useradd -m -s /bin/bash admin && install -d -m 700 -o admin -g admin /home/admin/.ssh \
 && echo 'admin ALL=(root) NOPASSWD: ALL' > /etc/sudoers.d/admin && chmod 440 /etc/sudoers.d/admin \
 && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]

ARG BUILD_FROM
FROM $BUILD_FROM
COPY rootfs /
COPY flipped_energy_au /integration/flipped_energy_au
RUN chmod a+x /etc/addon/run
CMD [ "/etc/addon/run" ]

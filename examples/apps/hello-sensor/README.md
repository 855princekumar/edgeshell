# hello-sensor

An example app for [Apps on the edge](../../../docs/APPS.md).

```
python3 app/agents/esapp/esapp.py pack examples/apps/hello-sensor
```

makes `hello-sensor-1.0.0.esapp`. Upload it on the Apps page and roll it out.
On a node: `curl 127.0.0.1:8095` shows the last reading, `journalctl -u es-hello-sensor` the log.

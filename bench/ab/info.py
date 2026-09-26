import sys, time
import meshtastic.serial_interface as s
i = s.SerialInterface(sys.argv[1]); time.sleep(1)
m = i.getMyNodeInfo(); md = i.metadata
print("fw", md.firmware_version if md else None, "num", hex(i.myInfo.my_node_num), m.get('user', {}).get('longName'))
l = i.localNode.localConfig.lora
print("preset", l.modem_preset, "region", l.region, "tx", l.tx_enabled, "pwr", l.tx_power, "ch", l.channel_num)
print("gps_mode", i.localNode.localConfig.position.gps_mode)
print("nodes", [(n.get('user',{}).get('shortName'), n.get('snr'), n.get('lastHeard')) for n in i.nodes.values()][:8])
i.close()

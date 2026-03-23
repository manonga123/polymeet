"""
test_micro.py — Diagnostic des microphones disponibles
Lance ce fichier pour voir tous les micros détectés sur ton PC.
"""
import pyaudio

pa = pyaudio.PyAudio()

print("\n" + "="*55)
print("  MICROPHONES DISPONIBLES SUR CET ORDINATEUR")
print("="*55)

found = []
for i in range(pa.get_device_count()):
    info = pa.get_device_info_by_index(i)
    if info['maxInputChannels'] > 0:
        found.append((i, info))
        print(f"\n  [{i}] {info['name']}")
        print(f"       Canaux  : {info['maxInputChannels']}")
        print(f"       Taux    : {int(info['defaultSampleRate'])} Hz")

if not found:
    print("\n  ❌ Aucun microphone détecté !")
else:
    print(f"\n{'='*55}")
    default = pa.get_default_input_device_info()
    print(f"  ✅ Micro par défaut : [{default['index']}] {default['name']}")
    print(f"{'='*55}\n")

pa.terminate()
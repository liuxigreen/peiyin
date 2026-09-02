import sqlite3, os
db = sqlite3.connect('/opt/peiyin/controlplane/dev.db', timeout=60)
n = db.execute("update pipeline_tasks set output_paths=json_remove(output_paths,'$.payload.ref_audio_b64') "
               "where output_paths like '%ref_audio_b64%'").rowcount
db.commit()
print('stripped rows:', n)
print('db size MB:', os.path.getsize('/opt/peiyin/controlplane/dev.db')//1048576)
print('vacuuming...')
db.execute('vacuum')
print('db size after vacuum MB:', os.path.getsize('/opt/peiyin/controlplane/dev.db')//1048576)
db.close()

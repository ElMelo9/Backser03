-- Crea un trabajo del Agente SQL Server que ejecuta subir_nube.bat todos los dias.
-- Sirve para SQL Server 2000 y 2005. Ejecutelo en el Analizador de consultas
-- (2000) o en Management Studio (2005) conectado al servidor.
--
-- Ajuste la ruta del .bat y la hora (@active_start_time, formato HHMMSS) antes
-- de ejecutarlo. Elija una hora posterior al termino de sus respaldos.
--
-- El paso falla (y el trabajo queda en rojo) si algun archivo no se pudo subir,
-- porque subir_nube.py termina con un codigo distinto de 0.
-- La cuenta de servicio del Agente SQL debe poder leer la carpeta de respaldos
-- y la carpeta de subir_nube, y tener salida a Internet.

USE msdb
GO

DECLARE @job_id UNIQUEIDENTIFIER

EXEC msdb.dbo.sp_add_job
    @job_name = N'Subir respaldos a la nube',
    @enabled = 1,
    @description = N'Copia los respaldos a Dropbox o Google Drive con subir_nube.py',
    @job_id = @job_id OUTPUT

EXEC msdb.dbo.sp_add_jobstep
    @job_id = @job_id,
    @step_name = N'Ejecutar subir_nube',
    @subsystem = N'CMDEXEC',
    @command = N'C:\SubirNube\subir_nube.bat',
    @cmdexec_success_code = 0,
    @on_success_action = 1,   -- terminar informando exito
    @on_fail_action = 2       -- terminar informando error

EXEC msdb.dbo.sp_add_jobschedule
    @job_id = @job_id,
    @name = N'Diario 03:30',
    @enabled = 1,
    @freq_type = 4,           -- diario
    @freq_interval = 1,       -- cada 1 dia
    @active_start_time = 33000

EXEC msdb.dbo.sp_add_jobserver
    @job_id = @job_id,
    @server_name = N'(local)'
GO

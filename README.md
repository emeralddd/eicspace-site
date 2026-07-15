<h1 align="center">
  <br>
  EICSpace: The Online Judge of EIC
</h1>
<p align="center">
  <a href="LICENSE.md">
    <img alt="License" src="https://img.shields.io/github/license/DMOJ/online-judge"/>
  </a>
</p>

EICSpace is an online judge serving the EIC. This was forked from the DMOJ platform, a modern open-source online judge and contest platform system.

See it live at [eicspace.tunglamnguyenkhac.com](https://eicspace.tunglamnguyenkhac.com/ "I will definitely purchase another domain to replace this long URL.")!

## Notes
- Add `DMOJ_PROBLEM_DATA_ROOT` in local_settings.py to assign the folder holding testcases.
- Edit Admin URL from `localhost:8081` to yours in Admin Dashboard -> Sites.
- Uncomment `EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'` to send email to the console, or fill a valid SMTP (or API) config to avoid registration error.
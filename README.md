# CryoSPARC_Disc_Space_Report
A script that, along with duc, associates CryoSPARC raw data and project data and reports space on disc as well as biggest jobs. 

Note 1: You must have duc (https://duc.zevv.nl/) downloaded and a recent disc space inventory done for this to be useful. 

Note 2: You also need to be in a conda environment that has pandas. Any relatively up to date conda environment (running python 3) that has pandas should do.

1. Query duc and write down all relevant file sizes to a file (takes ~30 s)
  ```
  duc ls -RF --dirs-only --ascii -d duc_db_location path_to_query output_file
  ```

2. Run cryosparc scrape on target directory (takes a few minutes; output will go to wherever duc_output.txt is)
  ```
  python3 path_to_script directory_to_query location_of_duc_output start_date end_date
  ```
  directory_to_query should be the parent directory of both the CS projects and the raw data
  start_date and end_date are the dates of CS projects (date the project was first generated) to be considered.
  
3. Visualize .csv with whatever spreadsheet viewer you use

----

Advanced Notes:

1. If you want to exclude folders ONLY for the finding of the CS projects, you can add them to the EXCLUDED_DIRS list. This may speed up the script significantly, depending on how big / extensive they are.

2. The script assumes that anything <0.1 TB is a folder that has been archived or deleted. If the raw data and project directories are <0.5 TB (combined) in size, they will not be shown on the list.

3. Any CS project >5 TB will have the largest jobs written in the comments section. Currently any job >0.5 TB is written down, along with its size. This can be changed by changing the SIZE_THRESHOLD_GB in the script.

----

Example:

<img width="1586" height="274" alt="image" src="https://github.com/user-attachments/assets/341fc95f-9b4d-4cbe-b915-60a4c5fcf158" />

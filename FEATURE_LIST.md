All the features below MUST exist and useable in this project, not just as a demo


1. Student registering, deafult with a command, there can be created a department, Reception department that can be created. An endpoint must exist to create departemnts, tell their job and all that. After reception department was created when installing the app, which i will do personally, there must be a way to create test or assing a test to upcoming new student. For example, I am the manager, I created a reception department, I pass them a test which they are required to show to student to when they are entering the education center. The test will be solved, it can be edit, it can be recreated by AI but only maanger acceptes it for the change, the results will be checked by AI or will be provided when test is adding, it will show the students level at right there and AI will suggest groups for the student, its that they can be left groupless, can leave without entering the education enrolling to a group or they can be added to group that is suggested by AI or they can be added to group chosen by the repectionist and if manager acceptets it, they will be added (this accpetence thing can be turned on and off so it stays dynamic). 

2. There must be a page for all student list, the page must continain important statics and filters like total cound of the students, students with groups, without groups, students who just left, students who just joined, filter that can be filterred by their level or month (both are created by hand on the api so they stay completly dynamic and fits every education), their age, their gender, their location, their teacher if they have one, their academic school (this info will be asked when they are enrolling to the education center), their branch, the time they joined, the custom dates, a comparison api to compare a week, day, momth or even a year or an hour to determine how much students joined in one day or month or year or even hour compated to the last months data. THey can be edited, added to group, blocked, removed from group, or any other acntions must be included in that api endpoints that I listend above. Also there should be no problem when student removed from the group WHILE the teacher was taking attendence. Think hard about race conditions and rate limiting. 

3. There must be a dashboard for the teacher that will show important stuff like how many students total he or she has, how many groups teacher has, how many level groups, which level groups, when they graduiate expected to graduate, when is their exam, when is their meeting, when is their next lesson, what kind of lesson (lesson types will be created by the managers like Video Lesson, Speaking Lesson, Main lesson, hangout lesson or any other kind so it stays dynamic.) The next meeting for the teachers from the staff, if there are forms which must be filled, there should be must be warnigns and all that kind of stuff that is needed by the teachers. the forms and questions anonmyies or not, they shoulld be able to fill it out and give them to the manager which can be anylazed, seen by the managers, and also AI can read them and give a anaylzies with actual charts and all that. 

4. Same for students, just like in task 3, students must be able to get everything, weater a form from teacher itself, or managers of the edu center and those stuff, they can see their homewokr, they can finish it as marked, they can send, ask, questions and images straight in the app to their teacher, multiple teachers and assistents can be assing to the same group without a problem. They should have access to libraryr of books that was accpeted by the teacher and manager they can access, not download but sdee the and use them (Both accpeteding can be turned off and on dyanmicly and also that download thing also can be turned off and on)

5. Department creation and auto splitting tasks via ai fairly or without. Tasks can be distrubied by manager/ceo onnly or whoever have that permission.

6. Individial staff also can get task from whoever has a permission.

7. There are types of giving task permissions. For example teacher can give a task to assistant or any lower department than teacher level, like levels are there too. For example there are grades like teacher is higher than assistant, mamnanger is higher than teafcher and asssitant, so that should be able to set to dynamic so when the project is installed to each educatiopn center they can apply their own rules. 

8. Recentionest or whoever has the permisson can access the test creation tools via mobile app, web cant nopt access bloicked by tenant, and there will be. (Test will be accepted, created, edited, and will be marked by ai or manager or whoever has the permision, default only manager). Once test starts the phone will be given to the student, and there will be time, thjey cant not dop anything except chosing the answers, Answer types can be different, multipole choice, True or False, writing, reading, listing, speaking, vocabulary, and more types, all can be set via manager, default is true false.

9. Material creation for the library will be created by AI, and it does not cover in the plans, its charged as you use.  

10. Auto sms will be send to all the phonme numnbers in the existing students fields. The date for the sms sending will be set dynamic and message will also be template with examples provided by low level ai. Dynamic sending data. 

12. Cards again check the current implemnentaiton and search for flaws. Cards canm be created, named by the manager or ceo ore whoever has the permissioon to edit them as well as create them. 

13. Fairness engine, check the fairness engine. The percetage of the ssalary will be set by the manager, and it will calculated.  Check the docs PRduction Vision for deatiled examplation below. 

14. Expenses must exist. So whoever has permission can create ean exepnse, and after the addition of the expnse,s it can be accpeted then the money will be givin in a type that is chosen like cash card or whatever exist, they all added dyanciely by the admin table. 

15. Attendce sheets page will exist on the student app. Cards connecltionj, classroom rank, status paid of the monthly thing. Custom achivbeets that are created by thew manager or the teacher or whoever has the permsion default manager and teacher. Manager can do it globally for all groups and teachers, teachers can only do their own, teachers can request for global anmd if its accpeted by the manager or whoever who ahs the permssion. Discoiunts that are given by the teacher, accepted by the manager or whoever who has permission. 

16. There must be a desktop app for the prineting jobs, so not all printers support networks so desktop app will get the jobs and evenly disturbe the jobs to all avaibale printes

17. Rewarding system for the teachers, managers can create rewards and give them to teachers as a reward, there are so many types of rewards like cash holdiay and alll that and cover system for teachers, teacher may leave or something may happen so manager or whoever can assing cover to teacher. 

18. Cover system has two types. One has globachat for all the teacher and teacher can ask for cover for their lesson in the period of time or accepted by the manager. 

19. Race conditions for the printers, cause look one printer avaible and two teacher press one button at the same time, all must be protected from race conditions. 

20. CEO must have the all the data related to the entire eudcation center and all aviable branches,. a so one manager may hold one or two branches so yeah you know what happens. 

21. Anyone who is staff can ask for loan direcntly from the manager and if accepted the cashier will get a notification and will give t he monme weatierh its cash or card. 

22. Focus on high performance and every metric that is presendted in every list all must be accurate, check for n+1 queioers and all

23. If student is considred absent with a reasson or without a reason it should be cut off from their total payment, It can be set from the manager weither it will be given a discount for absent or not by the manager. comecause some education centers may not like to just give a money discount for absencen without reason. 

24. Law breaking system, law will be uploaded by the managers or ceo if they break the rule staff or stdents they wioll be penenizeld accpordonmg to the laws. 

25. 
from app.services.todoist import list_projects, add_task
print(list_projects()[:3])
t = add_task("test from bot", project_id=2366738594)
print("added:", t)
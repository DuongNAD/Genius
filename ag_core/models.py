from pydantic import BaseModel, Field
from typing import List, Optional

class DesignFile(BaseModel):
    path: str = Field(
        ..., 
        description="Target file path relative to the project root directory (e.g. 'src/app.py')"
    )
    specification: str = Field(
        ..., 
        description="Detailed specifications, functional requirements, and architecture constraints for this specific file."
    )

class DesignPlan(BaseModel):
    project_name: Optional[str] = Field(
        default="project", 
        description="A unique, URL-safe slug identifying the project."
    )
    description: Optional[str] = Field(
        default="High-level architectural description.", 
        description="High-level architectural description, component interactions, and overall system design."
    )
    files: List[DesignFile] = Field(
        default_factory=list, 
        description="A list of source code/configuration files that must be implemented to fulfill the design."
    )
